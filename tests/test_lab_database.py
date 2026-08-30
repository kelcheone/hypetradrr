import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.lab_database import deserialize_portfolio, serialize_portfolio
from trade_simulation.portfolio import Action, Instrument, LegIntent, MarketSnapshot, OrderIntent, PaperBroker, Portfolio, Quote, Side


class LabDatabaseTests(TestCase):
    def test_portfolio_state_survives_json_round_trip(self) -> None:
        btc = Instrument("BTC", "PERP")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        broker = PaperBroker(taker_fee=Decimal("0.001"), maker_fee=Decimal("0"), slippage=Decimal("0"))
        portfolio, _ = broker.execute(
            Portfolio.create("trend-breakout", Decimal("10000")),
            OrderIntent(Action.OPEN, (LegIntent(btc, Side.LONG, Decimal("1000")),), "breakout"),
            MarketSnapshot(now, {btc: Quote(Decimal("100"), Decimal("100"))}),
        )

        restored = deserialize_portfolio(json.dumps(serialize_portfolio(portfolio)))

        self.assertEqual(restored, portfolio)
