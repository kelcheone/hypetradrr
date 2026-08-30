from datetime import datetime, timezone
from decimal import Decimal
from unittest import IsolatedAsyncioTestCase, TestCase

from trade_simulation.lab_config import LabSettings
from trade_simulation.lab_service import BTC_PERP, LabRuntime, build_lab, experiment_configuration
from trade_simulation.market import Candle
from trade_simulation.portfolio import Action, LegIntent, MarketSnapshot, OrderIntent, PaperBroker, Quote, Side


def settings() -> LabSettings:
    return LabSettings(
        "postgresql://unused", True, Decimal("10000"), 30,
        Decimal(".00045"), Decimal(".00015"), Decimal(".0007"), Decimal(".0004"),
        Decimal(".0001"), Decimal("2"), Decimal(".02"), Decimal(".15"),
        10, "/tmp", "127.0.0.1", 8000,
    )


class LabServiceTests(TestCase):
    def test_frozen_configuration_contains_all_seven_strategies(self) -> None:
        lab = build_lab(settings())
        configuration = experiment_configuration(settings(), lab, "@142")
        self.assertEqual(len(configuration["strategies"]), 7)
        self.assertEqual(len({item["key"] for item in configuration["strategies"]}), 7)

    def test_restart_recovers_each_funding_settlement_once(self) -> None:
        runtime = LabRuntime(settings())
        runtime.lab.broker = PaperBroker(
            taker_fee=Decimal(0), maker_fee=Decimal(0), slippage=Decimal(0),
        )
        opened_at = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        portfolio, _ = runtime.lab.broker.execute(
            runtime.lab.portfolios["trend-breakout"],
            OrderIntent(Action.OPEN, (LegIntent(BTC_PERP, Side.SHORT, Decimal("1000")),), "test"),
            MarketSnapshot(opened_at, {BTC_PERP: Quote(Decimal("100"), Decimal("100"))}),
        )
        runtime.lab.portfolios["trend-breakout"] = portfolio
        settlement = opened_at.replace(hour=13)
        runtime._funding_rows = {BTC_PERP: ((settlement, Decimal(".001")),)}
        runtime.market.seed_candles((Candle(
            BTC_PERP, opened_at, 60, Decimal("100"), Decimal("112"),
            Decimal("99"), Decimal("110"), Decimal("1"),
        ),))
        snapshot = MarketSnapshot(
            opened_at.replace(hour=14), {BTC_PERP: Quote(Decimal("120"), Decimal("120"))},
        )

        runtime._recover_funding(snapshot)
        recovered = runtime.lab.portfolios["trend-breakout"]
        self.assertEqual(recovered.balance, Decimal("10001.1"))
        runtime._recover_funding(snapshot)
        self.assertEqual(runtime.lab.portfolios["trend-breakout"].balance, recovered.balance)


class CompletionTests(IsolatedAsyncioTestCase):
    async def test_completion_persists_forced_exit_event(self) -> None:
        runtime = LabRuntime(settings())
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        snapshot = MarketSnapshot(timestamp, {BTC_PERP: Quote(Decimal("9999"), Decimal("10001"))})
        key = "trend-breakout"
        portfolio, _ = runtime.lab.broker.execute(
            runtime.lab.portfolios[key],
            OrderIntent(Action.OPEN, (LegIntent(BTC_PERP, Side.LONG, Decimal("1000")),), "test"),
            snapshot,
        )
        runtime.lab.portfolios[key] = portfolio
        runtime.experiment = {"id": "lab-1", "status": "RUNNING"}

        class FakeDatabase:
            events = []

            async def save_step(self, _id, _snapshot, _lab, events):
                self.events = events

            async def complete(self, _id, _timestamp):
                pass

            async def experiment(self, _id):
                return {"id": _id, "status": "COMPLETED"}

        database = FakeDatabase()
        runtime.database = database
        await runtime._complete(snapshot)

        self.assertEqual(database.events[-1].kind, "TRADE_CLOSED")
        self.assertEqual(database.events[-1].message, "experiment end")
