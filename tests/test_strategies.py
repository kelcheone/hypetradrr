from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.market import Candle
from trade_simulation.portfolio import Action, Instrument, LegIntent, MarketSnapshot, OrderType, Portfolio, Quote, Side
from trade_simulation.strategies import BuyAndHold, CashBenchmark, FundingCarry, MakerMeanReversion, PairsMeanReversion, StrategyContext, TrendBreakout, VolatilityBreakout


def candle(instrument: Instrument, start: datetime, close: str, high: str | None = None, low: str | None = None, volume: str = "10", minutes: int = 60) -> Candle:
    price = Decimal(close)
    return Candle(
        instrument,
        start,
        minutes,
        price,
        Decimal(high or close),
        Decimal(low or close),
        price,
        Decimal(volume),
    )


class StrategyTests(TestCase):
    def test_trend_breakout_opens_after_prior_24_hour_high(self) -> None:
        btc = Instrument("BTC", "PERP")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = tuple(candle(btc, start + timedelta(hours=index), "100", "101", "99") for index in range(24))
        history += (candle(btc, start + timedelta(hours=24), "102", "102", "100"),)
        portfolio = Portfolio.create("trend-breakout", Decimal("10000"))
        context = StrategyContext(
            MarketSnapshot(history[-1].end, {btc: Quote(Decimal("101.9"), Decimal("102"))}),
            {(btc, 60): history},
            portfolio,
        )

        intent = TrendBreakout(btc).evaluate(context)

        self.assertEqual(intent.action, Action.OPEN)
        self.assertEqual(intent.legs[0].side, Side.LONG)

    def test_maker_reversion_posts_at_bid_after_extreme_vwap_deviation(self) -> None:
        btc = Instrument("BTC", "PERP")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = tuple(
            candle(btc, start + timedelta(minutes=index), "100", volume="1", minutes=1)
            for index in range(59)
        )
        history += (candle(btc, start + timedelta(minutes=59), "90", volume="1", minutes=1),)
        portfolio = Portfolio.create("maker-reversion", Decimal("10000"))
        context = StrategyContext(
            MarketSnapshot(history[-1].end, {btc: Quote(Decimal("90"), Decimal("90.1"))}),
            {(btc, 1): history},
            portfolio,
        )

        intent = MakerMeanReversion(btc).evaluate(context)

        self.assertEqual(intent.legs[0].side, Side.LONG)
        self.assertEqual(intent.legs[0].order_type, OrderType.POST_ONLY)
        self.assertEqual(intent.legs[0].limit_price, Decimal("90"))

    def test_pairs_strategy_shorts_rich_eth_and_longs_btc(self) -> None:
        btc = Instrument("BTC", "PERP")
        eth = Instrument("ETH", "PERP")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        btc_history = []
        eth_history = []
        for index in range(720):
            btc_price = Decimal("100") + Decimal(index) / 100
            noise = Decimal("1.005") if index % 2 else Decimal("0.995")
            eth_price = btc_price / 10 * noise
            btc_history.append(candle(btc, start + timedelta(hours=index), str(btc_price)))
            eth_history.append(candle(eth, start + timedelta(hours=index), str(eth_price)))
        btc_price = Decimal("107.20")
        eth_price = btc_price / 10 * Decimal("1.02")
        btc_history.append(candle(btc, start + timedelta(hours=720), str(btc_price)))
        eth_history.append(candle(eth, start + timedelta(hours=720), str(eth_price)))
        portfolio = Portfolio.create("btc-eth-pairs", Decimal("10000"))
        context = StrategyContext(
            MarketSnapshot(start + timedelta(hours=721), {
                btc: Quote(btc_price, btc_price),
                eth: Quote(eth_price, eth_price),
            }),
            {(btc, 60): tuple(btc_history), (eth, 60): tuple(eth_history)},
            portfolio,
        )

        intent = PairsMeanReversion(btc, eth).evaluate(context)

        sides = {leg.instrument.symbol: leg.side for leg in intent.legs}
        self.assertEqual(sides, {"BTC": Side.LONG, "ETH": Side.SHORT})

    def test_funding_carry_opens_only_when_projected_income_clears_costs(self) -> None:
        spot = Instrument("BTC", "SPOT")
        perp = Instrument("BTC", "PERP")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        portfolio = Portfolio.create("funding-carry", Decimal("10000"))
        market = MarketSnapshot(now, {
            spot: Quote(Decimal("100"), Decimal("100")),
            perp: Quote(Decimal("100"), Decimal("100")),
        })
        strategy = FundingCarry(
            spot, perp,
            roundtrip_cost=Decimal("0.002"),
            safety_buffer=Decimal("0.001"),
        )

        intent = strategy.evaluate(StrategyContext(
            market, {}, portfolio,
            {perp: (Decimal("0.0002"),) * 8},
        ))

        self.assertEqual(
            {(leg.instrument.kind, leg.side) for leg in intent.legs},
            {("SPOT", Side.LONG), ("PERP", Side.SHORT)},
        )
        self.assertTrue(all(leg.notional == Decimal("4500.00") for leg in intent.legs))

    def test_benchmarks_are_cash_or_one_time_spot_exposure(self) -> None:
        spot = Instrument("BTC", "SPOT")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        portfolio = Portfolio.create("btc-benchmark", Decimal("10000"))
        context = StrategyContext(
            MarketSnapshot(now, {spot: Quote(Decimal("100"), Decimal("100"))}),
            {}, portfolio,
        )

        self.assertIsNone(CashBenchmark().evaluate(context))
        intent = BuyAndHold(spot).evaluate(context)
        self.assertEqual(intent.legs[0], LegIntent(spot, Side.LONG, Decimal("10000")))
        self.assertGreater(intent.legs[0].notional, 0)
        self.assertLessEqual(intent.legs[0].notional, Decimal("20000"))

    def test_volatility_breakout_requires_compression_range_break_and_volume(self) -> None:
        btc = Instrument("BTC", "PERP")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        quiet = tuple(
            candle(
                btc, start + timedelta(minutes=15 * index), "100", "100.1", "99.9",
                minutes=15,
            )
            for index in range(672)
        )
        breakout = candle(
            btc, start + timedelta(minutes=15 * 672), "101", "101", "100",
            volume="20", minutes=15,
        )
        portfolio = Portfolio.create("volatility-breakout", Decimal("10000"))
        context = StrategyContext(
            MarketSnapshot(breakout.end, {btc: Quote(Decimal("100.9"), Decimal("101"))}),
            {(btc, 15): quiet + (breakout,)},
            portfolio,
        )

        intent = VolatilityBreakout(btc).evaluate(context)

        self.assertEqual(intent.action, Action.OPEN)
        self.assertEqual(intent.legs[0].side, Side.LONG)
