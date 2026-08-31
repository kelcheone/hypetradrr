from datetime import datetime, timezone
from decimal import Decimal
from unittest import TestCase

from trade_simulation.portfolio import (
    Action,
    Instrument,
    LegIntent,
    MarketSnapshot,
    OrderIntent,
    OrderType,
    PaperBroker,
    Portfolio,
    Quote,
    Side,
)


class PaperBrokerTests(TestCase):
    def test_market_trade_accounts_for_spread_fees_and_realized_pnl(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        btc = Instrument("BTC", "PERP")
        broker = PaperBroker(taker_fee=Decimal("0.01"), maker_fee=Decimal("0"), slippage=Decimal("0"))
        portfolio = Portfolio.create("trend", Decimal("10000"))

        portfolio, trade = broker.execute(
            portfolio,
            OrderIntent(Action.OPEN, (LegIntent(btc, Side.LONG, Decimal("1000")),), "breakout"),
            MarketSnapshot(now, {btc: Quote(Decimal("99"), Decimal("100"))}),
        )
        self.assertEqual(trade.legs[0].entry_price, Decimal("100"))
        self.assertEqual(portfolio.balance, Decimal("9990"))

        portfolio, trade = broker.execute(
            portfolio,
            OrderIntent(Action.CLOSE, reason="trailing stop"),
            MarketSnapshot(now, {btc: Quote(Decimal("110"), Decimal("111"))}),
        )
        self.assertEqual(trade.gross_pnl, Decimal("100"))
        self.assertEqual(trade.fees, Decimal("21"))
        self.assertEqual(portfolio.balance, Decimal("10079"))

    def test_multi_leg_trade_stays_delta_neutral_and_receives_short_funding(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        spot = Instrument("BTC", "SPOT")
        perp = Instrument("BTC", "PERP")
        broker = PaperBroker(
            taker_fee=Decimal("0.01"), maker_fee=Decimal("0"), slippage=Decimal("0"),
            spot_taker_fee=Decimal("0.02"),
        )
        portfolio = Portfolio.create("funding-carry", Decimal("10000"))
        portfolio, _ = broker.execute(
            portfolio,
            OrderIntent(Action.OPEN, (
                LegIntent(spot, Side.LONG, Decimal("500")),
                LegIntent(perp, Side.SHORT, Decimal("500")),
            ), "positive funding"),
            MarketSnapshot(now, {
                spot: Quote(Decimal("100"), Decimal("100")),
                perp: Quote(Decimal("100"), Decimal("100")),
            }),
        )

        moved = MarketSnapshot(now, {
            spot: Quote(Decimal("110"), Decimal("110")),
            perp: Quote(Decimal("110"), Decimal("110")),
        }, {perp: Decimal("0.001")})
        portfolio = broker.mark(portfolio, moved)
        self.assertEqual(portfolio.equity, Decimal("9985"))

        portfolio, payment = broker.settle_funding(portfolio, moved)
        self.assertEqual(payment, Decimal("0.550"))
        self.assertEqual(portfolio.balance, Decimal("9985.550"))
        self.assertEqual(portfolio.open_trade.funding, Decimal("0.550"))

        replayed, duplicate = broker.settle_funding(portfolio, moved)
        self.assertEqual(duplicate, Decimal("0"))
        self.assertEqual(replayed.balance, portfolio.balance)

    def test_post_only_limit_requires_trade_through_not_touch(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        btc = Instrument("BTC", "PERP")
        broker = PaperBroker(taker_fee=Decimal("0.01"), maker_fee=Decimal("0.001"), slippage=Decimal("0"))
        portfolio = Portfolio.create("maker-reversion", Decimal("10000"))
        intent = OrderIntent(Action.OPEN, (
            LegIntent(btc, Side.LONG, Decimal("1000"), OrderType.POST_ONLY, Decimal("100")),
        ), "oversold")

        unchanged, trade = broker.execute(
            portfolio,
            intent,
            MarketSnapshot(now, {btc: Quote(Decimal("100"), Decimal("101"))}, trade_ranges={btc: (Decimal("100"), Decimal("101"))}),
        )
        self.assertIsNone(trade)
        self.assertEqual(unchanged, portfolio)

        filled, trade = broker.execute(
            portfolio,
            intent,
            MarketSnapshot(now, {btc: Quote(Decimal("100"), Decimal("101"))}, trade_ranges={btc: (Decimal("99.5"), Decimal("101"))}),
        )
        self.assertEqual(trade.legs[0].entry_price, Decimal("100"))
        self.assertEqual(filled.balance, Decimal("9999"))
