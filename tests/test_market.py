from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.market import CandleBuilder
from trade_simulation.portfolio import Instrument


class CandleBuilderTests(TestCase):
    def test_rolls_deterministic_ohlcv_candles(self) -> None:
        btc = Instrument("BTC", "PERP")
        start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        builder = CandleBuilder(btc, minutes=1)

        self.assertIsNone(builder.add(start, Decimal("100"), Decimal("2")))
        self.assertIsNone(builder.add(start + timedelta(seconds=30), Decimal("103"), Decimal("1")))
        candle = builder.add(start + timedelta(minutes=1), Decimal("101"), Decimal("4"))

        self.assertEqual(
            (candle.open, candle.high, candle.low, candle.close, candle.volume),
            tuple(map(Decimal, ("100", "103", "100", "103", "3"))),
        )
        self.assertEqual(builder.current.open, Decimal("101"))

