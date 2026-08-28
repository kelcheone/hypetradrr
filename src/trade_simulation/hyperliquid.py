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

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"


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
