from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.market import CandleBuilder, LiveMarketState, MarketUpdate
from trade_simulation.portfolio import Instrument, Quote


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

    def test_live_state_uses_trade_ranges_once_and_settles_funding_hourly(self) -> None:
        perp = Instrument("BTC", "PERP")
        spot = Instrument("BTC", "SPOT")
        start = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        state = LiveMarketState((perp, spot))

        self.assertIsNone(state.apply(MarketUpdate(start, perp, quote=Quote(Decimal("99"), Decimal("101")))))
        snapshot = state.apply(MarketUpdate(start, spot, quote=Quote(Decimal("99"), Decimal("101"))))
        self.assertEqual(len(snapshot.quotes), 2)

        trade_snapshot = state.apply(MarketUpdate(start, perp, trade_range=(Decimal("98"), Decimal("100"))))
        self.assertEqual(trade_snapshot.trade_ranges, {perp: (Decimal("98"), Decimal("100"))})
        self.assertEqual(state.snapshot(start).trade_ranges, {})

        self.assertEqual(state.apply(MarketUpdate(start, perp, funding_rate=Decimal("0.001"))).funding, {})
        settlement = state.apply(MarketUpdate(start + timedelta(hours=1), perp, funding_rate=Decimal("0.002")))
        self.assertEqual(settlement.funding, {perp: Decimal("0.001")})
