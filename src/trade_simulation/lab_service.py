from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from .hyperliquid import candle_history, historical_funding, lab_market_events, spot_coin
from .lab import Lab, LabEvent
from .lab_config import LabSettings
from .lab_database import LabDatabase
from .market import LiveMarketState, MarketUpdate
from .portfolio import Action, Instrument, MarketSnapshot, OrderIntent, PaperBroker, Quote, RiskPolicy
from .strategies import (
    BuyAndHold,
    CashBenchmark,
    FundingCarry,
    MakerMeanReversion,
    PairsMeanReversion,
    Strategy,
    TrendBreakout,
    VolatilityBreakout,
)


BTC_PERP = Instrument("BTC", "PERP")
ETH_PERP = Instrument("ETH", "PERP")
BTC_SPOT = Instrument("BTC", "SPOT")


def build_lab(settings: LabSettings) -> Lab:
    strategies: tuple[Strategy, ...] = (
        CashBenchmark(),
        BuyAndHold(BTC_SPOT),
        TrendBreakout(BTC_PERP),
        VolatilityBreakout(BTC_PERP),
        MakerMeanReversion(BTC_PERP),
        PairsMeanReversion(BTC_PERP, ETH_PERP),
        FundingCarry(BTC_SPOT, BTC_PERP, settings.carry_roundtrip_cost),
    )
    return Lab(
        strategies,
        starting_balance=settings.starting_balance,
        broker=PaperBroker(
            taker_fee=settings.perp_taker_fee,
            maker_fee=settings.perp_maker_fee,
            spot_taker_fee=settings.spot_taker_fee,
            spot_maker_fee=settings.spot_maker_fee,
            slippage=settings.slippage,
        ),
        risk=RiskPolicy(settings.max_gross, settings.daily_loss, settings.max_drawdown),
    )


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (Decimal, timedelta)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def experiment_configuration(settings: LabSettings, lab: Lab, spot_market: str) -> dict[str, object]:
    return {
        "settings": settings.snapshot(),
        "spot_market": spot_market,
        "strategies": [
            {"key": strategy.key, "parameters": _json_value(strategy)}
            for strategy in lab.strategies
        ],
    }


class LabRuntime:
    def __init__(self, settings: LabSettings) -> None:
        self.settings = settings
        self.database = LabDatabase(settings.database_url)
        self.lab = build_lab(settings)
        self.market = LiveMarketState((BTC_PERP, ETH_PERP, BTC_SPOT))
        self.experiment: dict[str, Any] | None = None
        self.spot_market = ""
        self.current_snapshot = None
        self.feed_connected = False
        self.feed_error: str | None = None
        self._funding_rows: dict[Instrument, tuple[tuple[datetime, Decimal], ...]] = {}
        self._funding_recovered = True
        self._last_saved_second: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.database.connect()
        self.spot_market = await spot_coin()
        self.experiment = await self.database.running_experiment()
        if self.experiment:
            self.lab.portfolios = await self.database.load_portfolios(self.experiment["id"])
            self._funding_recovered = False
            await self.database.log_event(self.experiment["id"], LabEvent(
                datetime.now(timezone.utc), "SYSTEM", "APPLICATION_RESTART", "Lab resumed",
            ))
        await self._warm_history()
        self._task = asyncio.create_task(self._run(), name="multi-strategy-paper-lab")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.database.close()

    async def _warm_history(self) -> None:
        now = datetime.now(timezone.utc)
        end_ms = int(now.timestamp() * 1000)
        recovery_start = now - timedelta(hours=24)
        for portfolio in self.lab.portfolios.values():
            trade = portfolio.open_trade
            if not trade:
                continue
            for leg in trade.legs:
                if leg.instrument.kind == "PERP":
                    recovery_start = min(recovery_start, leg.funding_settled_at or trade.opened_at)
        btc_1m, btc_15m, btc_1h, eth_1h, btc_funding, eth_funding = await asyncio.gather(
            candle_history("BTC", "1m", int((now - timedelta(hours=2)).timestamp() * 1000), end_ms),
            candle_history("BTC", "15m", int((now - timedelta(days=8)).timestamp() * 1000), end_ms),
            candle_history("BTC", "1h", int((now - timedelta(days=31)).timestamp() * 1000), end_ms),
            candle_history("ETH", "1h", int((now - timedelta(days=31)).timestamp() * 1000), end_ms),
            historical_funding("BTC", int(recovery_start.timestamp() * 1000), end_ms),
            historical_funding("ETH", int(recovery_start.timestamp() * 1000), end_ms),
        )
        for candles in (btc_1m, btc_15m, btc_1h, eth_1h):
            self.market.seed_candles(tuple(item for item in candles if item.end <= now))
        self._funding_rows = {BTC_PERP: btc_funding, ETH_PERP: eth_funding}
        self.market.seed_funding(BTC_PERP, tuple(rate for _, rate in btc_funding)[-168:])
        self.market.seed_funding(ETH_PERP, tuple(rate for _, rate in eth_funding)[-168:])

    async def _run(self) -> None:
        delay = 1
        instruments = {"BTC": BTC_PERP, "ETH": ETH_PERP, self.spot_market: BTC_SPOT}
        while True:
            try:
                async for update in lab_market_events(
                    instruments,
                    {"BTC": ("1m", "15m", "1h"), "ETH": ("1h",)},
                    stale_seconds=self.settings.stale_seconds,
                    required_bbo_coins=("BTC", "ETH", self.spot_market),
                ):
                    reconnected = not self.feed_connected
                    self.feed_connected = True
                    self.feed_error = None
                    delay = 1
                    if reconnected and self.experiment:
                        await self.database.log_event(self.experiment["id"], LabEvent(
                            update.timestamp, "SYSTEM", "MARKET_FEED_CONNECTED", "Hyperliquid feed connected",
                        ))
                    await self._on_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.feed_connected = False
                self.feed_error = str(exc)
                if self.experiment:
                    await self.database.log_event(self.experiment["id"], LabEvent(
                        datetime.now(timezone.utc), "SYSTEM", "MARKET_FEED_ERROR", str(exc),
                    ))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _on_update(self, update: MarketUpdate) -> None:
        snapshot = self.market.apply(update)
        if update.candle and self.experiment:
            await self.database.save_candle(self.experiment["id"], update.candle)
        if not snapshot:
            return
        self.current_snapshot = snapshot
        if not self.experiment:
            self.experiment = await self.database.running_experiment()
            if self.experiment:
                self.lab.portfolios = await self.database.load_portfolios(self.experiment["id"])
                self._funding_recovered = True
        if not self.experiment or self.experiment["status"] != "RUNNING":
            return
        if snapshot.timestamp >= self.experiment["ends_at"]:
            await self._complete(snapshot)
            return
        event_index = len(self.lab.events)
        if not self._funding_recovered:
            self._recover_funding(snapshot)
            self._funding_recovered = True
        self.lab.step(
            snapshot, self.market.history, self.market.funding_history,
            evaluate_keys=self._evaluation_keys(update),
        )
        events = self.lab.events[event_index:]
        second = snapshot.timestamp.replace(microsecond=0)
        if events or update.candle or snapshot.funding or second != self._last_saved_second:
            await self.database.save_step(self.experiment["id"], snapshot, self.lab, events)
            self._last_saved_second = second

    async def _complete(self, snapshot) -> None:
        event_index = len(self.lab.events)
        for key, portfolio in tuple(self.lab.portfolios.items()):
            if portfolio.pending_order:
                portfolio = replace(portfolio, pending_order=None)
            if portfolio.open_trade:
                portfolio, trade = self.lab.broker.execute(
                    portfolio, OrderIntent(Action.CLOSE, reason="experiment end"), snapshot,
                )
                assert trade is not None
                self.lab.events.append(LabEvent(
                    snapshot.timestamp, key, "TRADE_CLOSED", "experiment end", trade.id,
                ))
            self.lab.portfolios[key] = portfolio
        await self.database.save_step(
            self.experiment["id"], snapshot, self.lab, self.lab.events[event_index:],
        )
        await self.database.complete(self.experiment["id"], snapshot.timestamp)
        self.experiment = await self.database.experiment(self.experiment["id"])

    def _recover_funding(self, snapshot: MarketSnapshot) -> None:
        for key, portfolio in tuple(self.lab.portfolios.items()):
            trade = portfolio.open_trade
            if not trade:
                continue
            peak_equity = portfolio.peak_equity
            for leg in trade.legs:
                if leg.instrument.kind != "PERP":
                    continue
                after = leg.funding_settled_at or trade.opened_at
                for timestamp, rate in self._funding_rows.get(leg.instrument, ()):
                    if timestamp <= after or timestamp > snapshot.timestamp:
                        continue
                    candles = self.market.history.get((leg.instrument, 60), ())
                    candle = next((item for item in reversed(candles) if item.end <= timestamp), None)
                    price = candle.close if candle else (snapshot.quotes[leg.instrument].bid + snapshot.quotes[leg.instrument].ask) / 2
                    quotes = snapshot.quotes | {leg.instrument: Quote(price, price)}
                    portfolio, payment = self.lab.broker.settle_funding(
                        portfolio,
                        MarketSnapshot(timestamp, quotes, {leg.instrument: rate}),
                    )
                    if payment:
                        self.lab.events.append(LabEvent(
                            timestamp, key, "FUNDING_RECOVERED", str(payment), trade.id,
                        ))
            portfolio = self.lab.broker.mark(portfolio, snapshot)
            portfolio = replace(
                portfolio, peak_equity=max(peak_equity, portfolio.equity),
            )
            self.lab.portfolios[key] = portfolio

    def _evaluation_keys(self, update: MarketUpdate) -> set[str]:
        keys = {"btc-benchmark", "funding-carry"}
        candle = update.candle
        if not candle:
            return keys
        if candle.instrument == BTC_PERP and candle.minutes == 1:
            keys.add("maker-reversion")
        if candle.instrument == BTC_PERP and candle.minutes == 15:
            keys.add("volatility-breakout")
        if candle.instrument == BTC_PERP and candle.minutes == 60:
            keys.add("trend-breakout")
        if candle.minutes == 60:
            btc = self.market.history.get((BTC_PERP, 60), ())
            eth = self.market.history.get((ETH_PERP, 60), ())
            if btc and eth and btc[-1].end == eth[-1].end:
                keys.add("btc-eth-pairs")
        return keys

    def feed_status(self) -> dict[str, object]:
        age = None
        last = None
        if self.current_snapshot:
            last = self.current_snapshot.timestamp
            age = max(0, (datetime.now(timezone.utc) - last).total_seconds())
        return {
            "connected": self.feed_connected,
            "last_update": last,
            "age_seconds": age,
            "error": self.feed_error,
            "quotes": {
                f"{instrument.symbol}-{instrument.kind}": {"bid": quote.bid, "ask": quote.ask}
                for instrument, quote in self.market.quotes.items()
            },
        }
