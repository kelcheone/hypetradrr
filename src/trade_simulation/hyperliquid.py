from __future__ import annotations

import asyncio
import json
import ssl
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator

import httpx
import websockets
import certifi

from .engine import Price
from .market import Candle, MarketUpdate
from .portfolio import Instrument, Quote

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"


def _interval_minutes(value: str) -> int:
    unit = value[-1]
    amount = int(value[:-1])
    return amount * {"m": 1, "h": 60, "d": 1440, "w": 10080}[unit]


class HyperliquidParser:
    """Normalize public Hyperliquid messages and emit only completed candles."""

    def __init__(self, instruments: dict[str, Instrument]) -> None:
        self.instruments = instruments
        self._candles: dict[tuple[str, str], Candle] = {}

    def parse(self, message: dict[str, object]) -> tuple[MarketUpdate, ...]:
        channel = message.get("channel")
        data = message.get("data")
        if channel == "bbo" and isinstance(data, dict):
            instrument = self.instruments[str(data["coin"])]
            bid, ask = data["bbo"]
            if not bid or not ask:
                return ()
            timestamp = datetime.fromtimestamp(int(data["time"]) / 1000, tz=timezone.utc)
            return (MarketUpdate(
                timestamp,
                instrument,
                quote=Quote(Decimal(bid["px"]), Decimal(ask["px"])),
            ),)
        if channel == "trades" and isinstance(data, list) and data:
            by_coin: dict[str, list[dict[str, object]]] = {}
            for item in data:
                by_coin.setdefault(str(item["coin"]), []).append(item)
            updates = []
            for coin, trades in by_coin.items():
                prices = [Decimal(str(item["px"])) for item in trades]
                timestamp = datetime.fromtimestamp(
                    max(int(item["time"]) for item in trades) / 1000,
                    tz=timezone.utc,
                )
                updates.append(MarketUpdate(
                    timestamp,
                    self.instruments[coin],
                    trade_range=(min(prices), max(prices)),
                    traded_volume=sum((Decimal(str(item["sz"])) for item in trades), Decimal("0")),
                ))
            return tuple(updates)
        if channel == "activeAssetCtx" and isinstance(data, dict):
            instrument = self.instruments[str(data["coin"])]
            return (MarketUpdate(
                datetime.now(timezone.utc),
                instrument,
                funding_rate=Decimal(str(data["ctx"]["funding"])),
            ),)
        if channel == "candle" and isinstance(data, dict):
            coin = str(data["s"])
            interval = str(data["i"])
            instrument = self.instruments[coin]
            candle = Candle(
                instrument,
                datetime.fromtimestamp(int(data["t"]) / 1000, tz=timezone.utc),
                _interval_minutes(interval),
                Decimal(str(data["o"])),
                Decimal(str(data["h"])),
                Decimal(str(data["l"])),
                Decimal(str(data["c"])),
                Decimal(str(data["v"])),
            )
            key = coin, interval
            previous = self._candles.get(key)
            self._candles[key] = candle
            return (MarketUpdate(previous.end, instrument, candle=previous),) if previous and candle.start > previous.start else ()
        return ()


async def margin_metadata(symbol: str) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(INFO_URL, json={"type": "meta"})
        response.raise_for_status()
        metadata = response.json()
    asset = next((item for item in metadata["universe"] if item["name"] == symbol), None)
    if not asset:
        raise RuntimeError(f"{symbol} perpetual is absent from Hyperliquid metadata")
    table_id = asset["marginTableId"]
    table = next((table for key, table in metadata.get("marginTables", []) if key == table_id), None)
    tiers = table["marginTiers"] if table else [{"lowerBound": "0", "maxLeverage": asset["maxLeverage"]}]
    first_max_leverage = int(tiers[0]["maxLeverage"])
    return {
        "symbol": symbol,
        "max_leverage": asset["maxLeverage"],
        "size_decimals": asset["szDecimals"],
        "margin_table_id": table_id,
        "margin_tiers": tiers,
        "maintenance_rate": str(Decimal(1) / (2 * first_max_leverage)),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


async def spot_coin(token_name: str = "UBTC") -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(INFO_URL, json={"type": "spotMeta"})
        response.raise_for_status()
        metadata = response.json()
    token = next((item for item in metadata["tokens"] if item["name"] == token_name), None)
    if not token:
        raise RuntimeError(f"Hyperliquid spot token {token_name} is unavailable")
    pair = next(
        (item for item in metadata["universe"] if item["tokens"] == [token["index"], 0]),
        None,
    )
    if not pair:
        raise RuntimeError(f"Hyperliquid {token_name}/USDC spot pair is unavailable")
    return str(pair["name"])


async def candle_history(coin: str, interval: str, start_ms: int, end_ms: int) -> tuple[Candle, ...]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(INFO_URL, json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        })
        response.raise_for_status()
        rows = response.json()
    instrument = Instrument(coin, "PERP")
    return tuple(Candle(
        instrument,
        datetime.fromtimestamp(int(row["t"]) / 1000, tz=timezone.utc),
        _interval_minutes(interval),
        Decimal(str(row["o"])), Decimal(str(row["h"])), Decimal(str(row["l"])),
        Decimal(str(row["c"])), Decimal(str(row["v"])),
    ) for row in rows)


async def historical_funding(coin: str, start_ms: int, end_ms: int) -> tuple[tuple[datetime, Decimal], ...]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(INFO_URL, json={
            "type": "fundingHistory", "coin": coin,
            "startTime": start_ms, "endTime": end_ms,
        })
        response.raise_for_status()
        rows = response.json()
    return tuple(
        (datetime.fromtimestamp(int(row["time"]) / 1000, tz=timezone.utc), Decimal(str(row["fundingRate"])))
        for row in rows
    )


async def lab_market_events(
    instruments: dict[str, Instrument],
    candle_subscriptions: dict[str, tuple[str, ...]],
    *,
    stale_seconds: int,
    required_bbo_coins: tuple[str, ...],
    trade_coins: tuple[str, ...] = ("BTC",),
    funding_coins: tuple[str, ...] = ("BTC", "ETH"),
) -> AsyncIterator[MarketUpdate]:
    parser = HyperliquidParser(instruments)
    tls = ssl.create_default_context(cafile=certifi.where())
    async with websockets.connect(
        WS_URL, ssl=tls, ping_interval=20, ping_timeout=10, close_timeout=5,
    ) as socket:
        subscriptions = [
            {"type": "bbo", "coin": coin} for coin in instruments
        ] + [
            {"type": "trades", "coin": coin} for coin in trade_coins
        ] + [
            {"type": "activeAssetCtx", "coin": coin} for coin in funding_coins
        ] + [
            {"type": "candle", "coin": coin, "interval": interval}
            for coin, intervals in candle_subscriptions.items() for interval in intervals
        ]
        for subscription in subscriptions:
            await socket.send(json.dumps({"method": "subscribe", "subscription": subscription}))
        loop = asyncio.get_running_loop()
        connected_at = loop.time()
        last_bbo: dict[str, float] = {}
        while True:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=1)
            except TimeoutError:
                raw = None
            now = loop.time()
            stale = [
                coin for coin in required_bbo_coins
                if now - last_bbo.get(coin, connected_at) > stale_seconds
            ]
            if stale:
                raise RuntimeError(f"Hyperliquid BBO stale for {', '.join(stale)}")
            if raw is None:
                continue
            message = json.loads(raw)
            if message.get("channel") == "bbo" and isinstance(message.get("data"), dict):
                last_bbo[str(message["data"]["coin"])] = now
            for update in parser.parse(message):
                yield update


async def bbo_prices(symbol: str, stale_seconds: int) -> AsyncIterator[Price]:
    tls = ssl.create_default_context(cafile=certifi.where())
    async with websockets.connect(WS_URL, ssl=tls, ping_interval=None, close_timeout=5) as socket:
        await socket.send(json.dumps({
            "method": "subscribe", "subscription": {"type": "bbo", "coin": symbol}
        }))
        last_second: datetime | None = None
        while True:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=stale_seconds)
            except TimeoutError:
                await socket.send(json.dumps({"method": "ping"}))
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=5)
                except TimeoutError as exc:
                    raise RuntimeError(f"Hyperliquid BBO feed stale for {stale_seconds}s") from exc
            message = json.loads(raw)
            if message.get("channel") != "bbo":
                continue
            data = message["data"]
            bid, ask = data["bbo"]
            if not bid or not ask:
                continue
            timestamp = datetime.fromtimestamp(data["time"] / 1000, tz=timezone.utc).replace(microsecond=0)
            if timestamp == last_second:
                continue
            last_second = timestamp
            yield Price(timestamp, Decimal(bid["px"]), Decimal(ask["px"]))
