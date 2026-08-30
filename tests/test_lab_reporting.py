from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.lab_reporting import csv_text, dashboard_payload


class LabReportingTests(TestCase):
    def test_dashboard_ranks_strategies_and_exposes_real_costs(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state = {
            "strategy": "trend-breakout",
            "starting_balance": "10000",
            "balance": "10080",
            "equity": "10080",
            "peak_equity": "10200",
            "status": "ACTIVE",
            "pending_order": None,
            "trades": [{
                "id": "trade-1", "strategy": "trend-breakout",
                "opened_at": now.isoformat(), "closed_at": (now + timedelta(hours=1)).isoformat(),
                "entry_reason": "breakout", "exit_reason": "trailing stop", "status": "CLOSED",
                "gross_pnl": "100", "fees": "10", "funding": "-5", "net_pnl": "85",
                "legs": [],
            }],
        }
        rows = {
            "portfolios": [{"strategy_key": "trend-breakout", "state": state, "updated_at": now}],
            "equity": [
                {"strategy_key": "trend-breakout", "timestamp": now, "equity": Decimal("10000")},
                {"strategy_key": "trend-breakout", "timestamp": now + timedelta(minutes=1), "equity": Decimal("10200")},
                {"strategy_key": "trend-breakout", "timestamp": now + timedelta(minutes=2), "equity": Decimal("9900")},
            ],
            "events": [],
        }

        payload = dashboard_payload(
            {"id": "lab-1", "status": "RUNNING", "started_at": now, "ends_at": now + timedelta(days=30)},
            rows,
            {"connected": True, "quotes": {}},
        )

        strategy = payload["strategies"][0]
        self.assertEqual(strategy["net_pnl"], 85.0)
        self.assertEqual(strategy["fees"], 10.0)
        self.assertEqual(strategy["funding"], -5.0)
        self.assertAlmostEqual(strategy["max_drawdown"], 300 / 10200)
        self.assertEqual(payload["summary"]["closed_trades"], 1)
        self.assertEqual(payload["trades"][0]["strategy_key"], "trend-breakout")

    def test_csv_export_serializes_multi_leg_trades(self) -> None:
        text = csv_text([{"strategy": "pairs", "legs": [{"symbol": "BTC"}, {"symbol": "ETH"}]}])
        self.assertIn('""symbol"": ""BTC""', text)
