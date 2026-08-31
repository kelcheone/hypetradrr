from decimal import Decimal
from datetime import datetime, timezone
from unittest import TestCase

from trade_simulation.hyperliquid import HyperliquidParser
from trade_simulation.portfolio import Instrument


class HyperliquidParserTests(TestCase):
    def test_normalizes_bbo_trades_funding_and_completed_candles(self) -> None:
        btc = Instrument("BTC", "PERP")
        parser = HyperliquidParser({"BTC": btc})

        bbo = parser.parse({
            "channel": "bbo",
            "data": {"coin": "BTC", "time": 1000, "bbo": [{"px": "99"}, {"px": "101"}]},
        })
        self.assertEqual((bbo[0].quote.bid, bbo[0].quote.ask), (Decimal("99"), Decimal("101")))

        trades = parser.parse({
            "channel": "trades",
            "data": [
                {"coin": "BTC", "time": 1100, "px": "100", "sz": "2"},
                {"coin": "BTC", "time": 1200, "px": "102", "sz": "3"},
            ],
        })
        self.assertEqual(trades[0].trade_range, (Decimal("100"), Decimal("102")))

        funding = parser.parse({
            "channel": "activeAssetCtx",
            "data": {"coin": "BTC", "ctx": {"funding": "0.0001"}},
        })
        self.assertEqual(funding[0].funding_rate, Decimal("0.0001"))

        first = {"channel": "candle", "data": {"t": 0, "T": 59999, "s": "BTC", "i": "1m", "o": "99", "h": "102", "l": "98", "c": "101", "v": "5"}}
        second = {"channel": "candle", "data": {"t": 60000, "T": 119999, "s": "BTC", "i": "1m", "o": "101", "h": "103", "l": "100", "c": "102", "v": "4"}}
        self.assertEqual(parser.parse(first), ())
        completed = parser.parse(second)
        self.assertEqual((completed[0].candle.open, completed[0].candle.close), (Decimal("99"), Decimal("101")))
        self.assertEqual(completed[0].timestamp, datetime.fromtimestamp(60, tz=timezone.utc))
