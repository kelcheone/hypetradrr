from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.config import Settings
from trade_simulation.engine import (
    Direction,
    Price,
    ReversalStrategy,
    exit_fill,
    exit_reason,
    gross_pnl,
    isolated_liquidation,
    plan_fill,
    account_status,
    eligibility_reason,
)


def settings() -> Settings:
    return Settings.from_env()


class EngineTests(TestCase):
    def test_long_and_short_money_paths(self) -> None:
        quantity = Decimal("1")
        self.assertEqual(gross_pnl(quantity, Decimal("100000"), Decimal("100200"), Direction.LONG), 200)
        self.assertEqual(gross_pnl(quantity, Decimal("100000"), Decimal("99800"), Direction.SHORT), 200)
        self.assertEqual(exit_fill(Decimal("100000"), Direction.LONG, Decimal("0.0001")), 99990)
        self.assertEqual(exit_fill(Decimal("100000"), Direction.SHORT, Decimal("0.0001")), 100010)

    def test_cost_aware_risk_sizing_fits_five_x_account(self) -> None:
        config = settings()
        plan = plan_fill(
            equity=Decimal("10000"), leverage=5, group="RISK_NORMALIZED",
            market_price=Decimal("100000"), direction=Direction.LONG,
            settings=config, maintenance_rate=Decimal("0.0125"),
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertLessEqual(plan.margin + plan.entry_fee, Decimal("10000"))
        round_trip_stop_cost = plan.notional * (
            config.stop_loss + 2 * config.slippage + 2 * config.taker_fee
        )
        self.assertEqual(round(round_trip_stop_cost, 8), Decimal("100.00000000"))

    def test_directional_exits_and_liquidation(self) -> None:
        long_liq = isolated_liquidation(
            Decimal("100000"), Decimal("2500"), Decimal("0.8"), Decimal("0.0125"), Direction.LONG
        )
        short_liq = isolated_liquidation(
            Decimal("100000"), Decimal("2500"), Decimal("0.8"), Decimal("0.0125"), Direction.SHORT
        )
        self.assertLess(long_liq, 100000)
        self.assertGreater(short_liq, 100000)
        self.assertEqual(exit_reason(Decimal("100200"), Direction.LONG, Decimal("100200"), Decimal("99900"), long_liq), "TAKE_PROFIT")
        self.assertEqual(exit_reason(Decimal("100100"), Direction.SHORT, Decimal("99800"), Decimal("100100"), short_liq), "STOP_LOSS")

    def test_symmetric_strategy_signals(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        config = settings()
        long_strategy = ReversalStrategy(config)
        long_signals = []
        for index, price in enumerate(map(Decimal, ("100000", "99740", "99795"))):
            long_signals += long_strategy.on_price(Price(base + timedelta(seconds=index), price, price))
        self.assertEqual([signal.direction for signal in long_signals], [Direction.LONG])

        short_strategy = ReversalStrategy(config)
        short_signals = []
        for index, price in enumerate(map(Decimal, ("100000", "100270", "100210"))):
            short_signals += short_strategy.on_price(Price(base + timedelta(seconds=index), price, price))
        self.assertEqual([signal.direction for signal in short_signals], [Direction.SHORT])

    def test_signal_ids_are_retry_safe(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ids = []
        for _ in range(2):
            strategy = ReversalStrategy(settings())
            signals = []
            for index, price in enumerate(map(Decimal, ("100000", "99740", "99795"))):
                signals += strategy.on_price(Price(base + timedelta(seconds=index), price, price))
            ids.append(signals[0].id)
        self.assertEqual(ids[0], ids[1])

    def test_account_limits(self) -> None:
        config = settings()
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(eligibility_reason(
            status="ACTIVE", has_position=True, daily_trades=0,
            cooldown_until=None, now=now, max_trades=10,
        ), "POSITION_ALREADY_OPEN")
        self.assertEqual(eligibility_reason(
            status="ACTIVE", has_position=False, daily_trades=10,
            cooldown_until=None, now=now, max_trades=10,
        ), "DAILY_TRADE_LIMIT")
        self.assertEqual(eligibility_reason(
            status="ACTIVE", has_position=False, daily_trades=0,
            cooldown_until=now + timedelta(seconds=1), now=now, max_trades=10,
        ), "COOLDOWN")
        self.assertEqual(account_status(
            balance=Decimal("9699"), starting_balance=Decimal("10000"),
            daily_start_equity=Decimal("10000"), drawdown=Decimal("0.03"),
            liquidation=False, settings=config,
        ), "DAILY_STOPPED")
        self.assertEqual(account_status(
            balance=Decimal("7000"), starting_balance=Decimal("10000"),
            daily_start_equity=Decimal("10000"), drawdown=Decimal("0.30"),
            liquidation=False, settings=config,
        ), "DISABLED")
