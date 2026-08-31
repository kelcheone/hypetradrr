from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .portfolio import Instrument, MarketSnapshot, Quote


@dataclass(frozen=True, slots=True)
class MarketUpdate:
    timestamp: datetime
    instrument: Instrument
    quote: Quote | None = None
    trade_range: tuple[Decimal, Decimal] | None = None
    traded_volume: Decimal = Decimal("0")
    candle: Candle | None = None
    funding_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Candle:
    instrument: Instrument
    start: datetime
    minutes: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)


class CandleBuilder:
    """Aggregate ordered trades into one fixed-duration OHLCV series."""

    def __init__(self, instrument: Instrument, *, minutes: int) -> None:
        if minutes < 1:
            raise ValueError("candle duration must be at least one minute")
        self.instrument = instrument
        self.minutes = minutes
        self.current: Candle | None = None

    def add(self, timestamp: datetime, price: Decimal, volume: Decimal = Decimal("0")) -> Candle | None:
        if timestamp.tzinfo is None:
            raise ValueError("market timestamps must be timezone-aware")
        if price <= 0 or volume < 0:
            raise ValueError("price must be positive and volume cannot be negative")
        bucket_seconds = self.minutes * 60
        epoch = int(timestamp.timestamp())
        start = datetime.fromtimestamp(epoch - epoch % bucket_seconds, tz=timezone.utc)
        if self.current and start < self.current.start:
            raise ValueError("market observations must be ordered")
        if not self.current or start > self.current.start:
            completed = self.current
            self.current = Candle(
                self.instrument, start, self.minutes,
                price, price, price, price, volume,
            )
            return completed
        self.current = replace(
            self.current,
            high=max(self.current.high, price),
            low=min(self.current.low, price),
            close=price,
            volume=self.current.volume + volume,
        )
        return None


class LiveMarketState:
    """Build complete strategy snapshots from normalized live updates."""

    def __init__(self, instruments: tuple[Instrument, ...]) -> None:
        self.instruments = instruments
        self.quotes: dict[Instrument, Quote] = {}
        self.history: dict[tuple[Instrument, int], tuple[Candle, ...]] = {}
        self.funding_history: dict[Instrument, tuple[Decimal, ...]] = {}
        self.last_update: dict[Instrument, datetime] = {}
        self._funding_rate: dict[Instrument, Decimal] = {}
        self._funding_hour: dict[Instrument, datetime] = {}

    def seed_candles(self, candles: tuple[Candle, ...]) -> None:
        for item in candles:
            key = item.instrument, item.minutes
            existing = self.history.get(key, ())
            if not existing or item.start > existing[-1].start:
                self.history[key] = existing + (item,)

    def seed_funding(self, instrument: Instrument, rates: tuple[Decimal, ...]) -> None:
        self.funding_history[instrument] = rates

    def apply(self, update: MarketUpdate) -> MarketSnapshot | None:
        self.last_update[update.instrument] = update.timestamp
        funding: dict[Instrument, Decimal] = {}
        if update.quote:
            self.quotes[update.instrument] = update.quote
        if update.candle:
            self.seed_candles((update.candle,))
        if update.funding_rate is not None:
            hour = update.timestamp.replace(minute=0, second=0, microsecond=0)
            previous_hour = self._funding_hour.get(update.instrument)
            if previous_hour is not None and hour > previous_hour:
                settled = self._funding_rate[update.instrument]
                funding[update.instrument] = settled
                self.funding_history[update.instrument] = (
                    self.funding_history.get(update.instrument, ()) + (settled,)
                )[-168:]
            self._funding_hour[update.instrument] = hour
            self._funding_rate[update.instrument] = update.funding_rate
        return self.snapshot(
            update.timestamp,
            funding=funding,
            trade_ranges={update.instrument: update.trade_range} if update.trade_range else {},
        )

    def snapshot(
        self,
        timestamp: datetime,
        *,
        funding: dict[Instrument, Decimal] | None = None,
        trade_ranges: dict[Instrument, tuple[Decimal, Decimal]] | None = None,
    ) -> MarketSnapshot | None:
        if any(instrument not in self.quotes for instrument in self.instruments):
            return None
        return MarketSnapshot(
            timestamp,
            self.quotes.copy(),
            funding or {},
            trade_ranges or {},
        )
