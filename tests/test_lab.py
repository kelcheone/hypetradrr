from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.lab import Lab
from trade_simulation.portfolio import Action, Instrument, LegIntent, MarketSnapshot, OrderIntent, OrderType, PaperBroker, Quote, RiskPolicy, Side
from trade_simulation.strategies import BuyAndHold, CashBenchmark


class PassiveEntry:
    key = "passive-entry"

    def __init__(self, instrument: Instrument) -> None:
        self.instrument = instrument

    def evaluate(self, context):
        if context.portfolio.trades:
            return None
        return OrderIntent(Action.OPEN, (
            LegIntent(self.instrument, Side.LONG, Decimal("1000"), OrderType.POST_ONLY, Decimal("100")),
        ), "passive entry")


class MarketEntry:
    key = "market-entry"

    def __init__(self, instrument: Instrument) -> None:
        self.instrument = instrument

    def evaluate(self, context):
        if context.portfolio.trades:
            return None
        return OrderIntent(Action.OPEN, (
            LegIntent(self.instrument, Side.LONG, Decimal("10000")),
        ), "market entry")


class LabTests(TestCase):
    def test_same_market_replay_produces_same_portfolios(self) -> None:
        spot = Instrument("BTC", "SPOT")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        observations = (
            MarketSnapshot(start, {spot: Quote(Decimal("100"), Decimal("100"))}),
            MarketSnapshot(start + timedelta(hours=1), {spot: Quote(Decimal("110"), Decimal("110"))}),
        )

        def replay() -> Lab:
            lab = Lab(
                (CashBenchmark(), BuyAndHold(spot)),
                starting_balance=Decimal("10000"),
                broker=PaperBroker(taker_fee=Decimal("0"), maker_fee=Decimal("0"), slippage=Decimal("0")),
                risk=RiskPolicy(Decimal("2"), Decimal("0.02"), Decimal("0.15")),
            )
            for observation in observations:
                lab.step(observation, {})
            return lab

        first = replay()
        second = replay()

        self.assertEqual(first.portfolios, second.portfolios)
        self.assertEqual(first.portfolios["cash-benchmark"].equity, Decimal("10000"))
        self.assertEqual(first.portfolios["btc-benchmark"].equity, Decimal("11000"))
        self.assertEqual(len(first.portfolios["btc-benchmark"].trades), 1)

    def test_post_only_intent_rests_until_trade_through(self) -> None:
        btc = Instrument("BTC", "PERP")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lab = Lab(
            (PassiveEntry(btc),),
            starting_balance=Decimal("10000"),
            broker=PaperBroker(taker_fee=Decimal("0"), maker_fee=Decimal("0"), slippage=Decimal("0")),
            risk=RiskPolicy(Decimal("2"), Decimal("0.02"), Decimal("0.15")),
        )

        lab.step(MarketSnapshot(start, {btc: Quote(Decimal("100"), Decimal("101"))}), {})
        self.assertIsNotNone(lab.portfolios["passive-entry"].pending_order)
        self.assertEqual(lab.portfolios["passive-entry"].trades, ())

        lab.step(MarketSnapshot(
            start + timedelta(seconds=30),
            {btc: Quote(Decimal("100"), Decimal("101"))},
            trade_ranges={btc: (Decimal("99.5"), Decimal("100"))},
        ), {})
        self.assertIsNone(lab.portfolios["passive-entry"].pending_order)
        self.assertEqual(len(lab.portfolios["passive-entry"].trades), 1)

    def test_risk_stop_closes_open_trade_immediately(self) -> None:
        spot = Instrument("BTC", "SPOT")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lab = Lab(
            (MarketEntry(spot),),
            starting_balance=Decimal("10000"),
            broker=PaperBroker(taker_fee=Decimal("0"), maker_fee=Decimal("0"), slippage=Decimal("0")),
            risk=RiskPolicy(Decimal("2"), Decimal("0.02"), Decimal("0.15")),
        )
        lab.step(MarketSnapshot(start, {spot: Quote(Decimal("100"), Decimal("100"))}), {})

        lab.step(MarketSnapshot(
            start + timedelta(minutes=1), {spot: Quote(Decimal("97"), Decimal("97"))},
        ), {})

        portfolio = lab.portfolios["market-entry"]
        self.assertIsNone(portfolio.open_trade)
        self.assertEqual(portfolio.status, "DAILY_STOPPED")
        self.assertEqual(portfolio.trades[-1].exit_reason, "DAILY_STOPPED")

    def test_buy_and_hold_benchmark_is_not_stopped_by_strategy_risk(self) -> None:
        spot = Instrument("BTC", "SPOT")
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lab = Lab(
            (BuyAndHold(spot),), starting_balance=Decimal("10000"),
            broker=PaperBroker(taker_fee=Decimal(0), maker_fee=Decimal(0), slippage=Decimal(0)),
            risk=RiskPolicy(Decimal("2"), Decimal("0.02"), Decimal("0.15")),
        )
        lab.step(MarketSnapshot(start, {spot: Quote(Decimal("100"), Decimal("100"))}), {})
        lab.step(MarketSnapshot(start + timedelta(minutes=1), {
            spot: Quote(Decimal("50"), Decimal("50")),
        }), {})

        self.assertIsNotNone(lab.portfolios["btc-benchmark"].open_trade)
        self.assertEqual(lab.portfolios["btc-benchmark"].status, "ACTIVE")

    def test_runtime_can_limit_signal_evaluation_without_skipping_marks(self) -> None:
        spot = Instrument("BTC", "SPOT")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lab = Lab(
            (BuyAndHold(spot),), starting_balance=Decimal("10000"),
            broker=PaperBroker(taker_fee=Decimal(0), maker_fee=Decimal(0), slippage=Decimal(0)),
            risk=RiskPolicy(Decimal("2"), Decimal("0.02"), Decimal("0.15")),
        )

        lab.step(
            MarketSnapshot(now, {spot: Quote(Decimal("100"), Decimal("100"))}),
            {}, evaluate_keys=set(),
        )

        self.assertIsNone(lab.portfolios["btc-benchmark"].open_trade)
