from dataclasses import replace
from decimal import Decimal
from unittest import TestCase

from trade_simulation.portfolio import Action, Instrument, LegIntent, OrderIntent, Portfolio, RiskPolicy, Side


class RiskPolicyTests(TestCase):
    def test_rejects_excess_exposure_and_drawdown_through_one_gate(self) -> None:
        btc = Instrument("BTC", "PERP")
        policy = RiskPolicy(max_gross=Decimal("2"), daily_loss=Decimal("0.02"), max_drawdown=Decimal("0.15"))
        portfolio = Portfolio.create("trend-breakout", Decimal("10000"))
        oversized = OrderIntent(Action.OPEN, (LegIntent(btc, Side.LONG, Decimal("20001")),), "breakout")
        admitted = OrderIntent(Action.OPEN, (LegIntent(btc, Side.LONG, Decimal("20000")),), "breakout")

        self.assertEqual(policy.rejection(portfolio, oversized), "EXCESS_GROSS_EXPOSURE")
        self.assertIsNone(policy.rejection(portfolio, admitted))

        drawn_down = replace(portfolio, balance=Decimal("8400"), equity=Decimal("8400"))
        self.assertEqual(policy.rejection(drawn_down, admitted), "DRAWDOWN_LIMIT")

