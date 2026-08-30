from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.lab import Lab
from trade_simulation.portfolio import Instrument, MarketSnapshot, PaperBroker, Quote, RiskPolicy
from trade_simulation.strategies import BuyAndHold, CashBenchmark


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

