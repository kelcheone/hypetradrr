from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .portfolio import Instrument


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
