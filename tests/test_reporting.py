from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.reporting import monte_carlo, trade_metrics


class ReportingTests(TestCase):
    def test_metrics_include_costs_expectancy_and_streaks(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        trades = [
            {
                "net_pnl": Decimal("9"), "gross_pnl": Decimal("20"),
                "entry_fee": Decimal("4.5"), "exit_fee": Decimal("4.5"),
                "slippage_cost": Decimal("2"), "entry_timestamp": now,
                "exit_timestamp": now + timedelta(minutes=1), "exit_reason": "TAKE_PROFIT",
            },
            {
                "net_pnl": Decimal("-21"), "gross_pnl": Decimal("-10"),
                "entry_fee": Decimal("4.5"), "exit_fee": Decimal("4.5"),
                "slippage_cost": Decimal("2"), "entry_timestamp": now,
                "exit_timestamp": now + timedelta(minutes=2), "exit_reason": "STOP_LOSS",
            },
        ]
        metrics = trade_metrics(trades)
        self.assertEqual(metrics["fees"], Decimal("18"))
        self.assertEqual(metrics["expectancy"], Decimal("-6"))
        self.assertEqual(metrics["break_even_win_rate"], Decimal("0.7"))
        self.assertEqual(metrics["longest_losing_streak"], 1)

    def test_monte_carlo_is_deterministic(self) -> None:
        trades = [{"net_pnl": Decimal("10")}, {"net_pnl": Decimal("-5")}]
        first = monte_carlo(trades, Decimal("10000"), 7)
        second = monte_carlo(trades, Decimal("10000"), 7)
        self.assertEqual(first, second)
        self.assertIsNotNone(first)
