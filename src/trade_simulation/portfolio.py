from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5


ZERO = Decimal("0")
ONE = Decimal("1")


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> Decimal:
        return ONE if self is Side.LONG else -ONE


class Action(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class OrderType(StrEnum):
    MARKET = "MARKET"
    POST_ONLY = "POST_ONLY"


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in {"SPOT", "PERP"}:
            raise ValueError("instrument kind must be SPOT or PERP")


@dataclass(frozen=True, slots=True)
class Quote:
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        if self.bid <= ZERO or self.ask < self.bid:
            raise ValueError("quote must have positive bid and ask >= bid")


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    timestamp: datetime
    quotes: dict[Instrument, Quote]
    funding: dict[Instrument, Decimal] | None = None
    trade_ranges: dict[Instrument, tuple[Decimal, Decimal]] | None = None


@dataclass(frozen=True, slots=True)
class LegIntent:
    instrument: Instrument
    side: Side
    notional: Decimal
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.notional <= ZERO:
            raise ValueError("leg notional must be positive")
        if self.order_type is OrderType.POST_ONLY and not self.limit_price:
            raise ValueError("post-only leg requires a limit price")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    action: Action
    legs: tuple[LegIntent, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.action is Action.OPEN and not self.legs:
            raise ValueError("open intent requires at least one leg")
        if self.action is Action.CLOSE and self.legs:
            raise ValueError("close intent uses the portfolio's open trade")


@dataclass(frozen=True, slots=True)
class TradeLeg:
    instrument: Instrument
    side: Side
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    exit_price: Decimal | None = None
    exit_fee: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class Trade:
    id: str
    strategy: str
    opened_at: datetime
    entry_reason: str
    legs: tuple[TradeLeg, ...]
    status: str = "OPEN"
    closed_at: datetime | None = None
    exit_reason: str | None = None
    gross_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    net_pnl: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class Portfolio:
    strategy: str
    starting_balance: Decimal
    balance: Decimal
    equity: Decimal
    peak_equity: Decimal
    daily_start_equity: Decimal
    status: str = "ACTIVE"
    trades: tuple[Trade, ...] = ()

    @classmethod
    def create(cls, strategy: str, starting_balance: Decimal) -> Portfolio:
        if starting_balance <= ZERO:
            raise ValueError("starting balance must be positive")
        return cls(
            strategy, starting_balance, starting_balance, starting_balance,
            starting_balance, starting_balance,
        )

    @property
    def open_trade(self) -> Trade | None:
        return next((trade for trade in reversed(self.trades) if trade.status == "OPEN"), None)


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    max_gross: Decimal
    daily_loss: Decimal
    max_drawdown: Decimal

    def rejection(self, portfolio: Portfolio, intent: OrderIntent) -> str | None:
        if intent.action is Action.CLOSE:
            return None
        if portfolio.status != "ACTIVE":
            return portfolio.status
        if portfolio.open_trade:
            return "TRADE_ALREADY_OPEN"
        drawdown = (
            (portfolio.peak_equity - portfolio.equity) / portfolio.peak_equity
            if portfolio.peak_equity else ZERO
        )
        if drawdown >= self.max_drawdown:
            return "DRAWDOWN_LIMIT"
        if portfolio.equity <= portfolio.daily_start_equity * (ONE - self.daily_loss):
            return "DAILY_LOSS_LIMIT"
        if sum((leg.notional for leg in intent.legs), ZERO) > portfolio.equity * self.max_gross:
            return "EXCESS_GROSS_EXPOSURE"
        return None


class PaperBroker:
    """Simulate fills and accounting for single- and multi-leg paper trades."""

    def __init__(self, *, taker_fee: Decimal, maker_fee: Decimal, slippage: Decimal) -> None:
        if min(taker_fee, maker_fee, slippage) < ZERO:
            raise ValueError("fees and slippage cannot be negative")
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.slippage = slippage

    def execute(
        self, portfolio: Portfolio, intent: OrderIntent, market: MarketSnapshot
    ) -> tuple[Portfolio, Trade | None]:
        return (
            self._open(portfolio, intent, market)
            if intent.action is Action.OPEN
            else self._close(portfolio, intent, market)
        )

    def mark(self, portfolio: Portfolio, market: MarketSnapshot) -> Portfolio:
        trade = portfolio.open_trade
        if not trade:
            return replace(portfolio, equity=portfolio.balance)
        unrealized = ZERO
        for leg in trade.legs:
            quote = market.quotes[leg.instrument]
            mid = (quote.bid + quote.ask) / 2
            unrealized += leg.quantity * (mid - leg.entry_price) * leg.side.sign
        equity = portfolio.balance + unrealized
        return replace(portfolio, equity=equity, peak_equity=max(portfolio.peak_equity, equity))

    def settle_funding(
        self, portfolio: Portfolio, market: MarketSnapshot
    ) -> tuple[Portfolio, Decimal]:
        trade = portfolio.open_trade
        if not trade:
            return portfolio, ZERO
        rates = market.funding or {}
        payment = ZERO
        for leg in trade.legs:
            if leg.instrument.kind != "PERP" or leg.instrument not in rates:
                continue
            quote = market.quotes[leg.instrument]
            notional = leg.quantity * (quote.bid + quote.ask) / 2
            payment -= leg.side.sign * notional * rates[leg.instrument]
        funded = replace(
            trade,
            funding=trade.funding + payment,
            net_pnl=trade.net_pnl + payment,
        )
        trades = tuple(funded if item.id == funded.id else item for item in portfolio.trades)
        updated = replace(portfolio, balance=portfolio.balance + payment, trades=trades)
        return self.mark(updated, market), payment

    def _open(
        self, portfolio: Portfolio, intent: OrderIntent, market: MarketSnapshot
    ) -> tuple[Portfolio, Trade | None]:
        if portfolio.open_trade:
            raise ValueError("portfolio already has an open trade")
        legs = []
        for requested in intent.legs:
            quote = market.quotes[requested.instrument]
            if requested.order_type is OrderType.POST_ONLY:
                assert requested.limit_price is not None
                if (
                    requested.side is Side.LONG and requested.limit_price >= quote.ask
                    or requested.side is Side.SHORT and requested.limit_price <= quote.bid
                ):
                    raise ValueError("post-only limit would cross the spread")
                price_range = (market.trade_ranges or {}).get(requested.instrument)
                traded_through = price_range and (
                    requested.side is Side.LONG and price_range[0] < requested.limit_price
                    or requested.side is Side.SHORT and price_range[1] > requested.limit_price
                )
                if not traded_through:
                    return portfolio, None
                fill = requested.limit_price
                fee_rate = self.maker_fee
            else:
                market_price = quote.ask if requested.side is Side.LONG else quote.bid
                fill = market_price * (ONE + self.slippage * requested.side.sign)
                fee_rate = self.taker_fee
            legs.append(TradeLeg(
                requested.instrument,
                requested.side,
                requested.notional / fill,
                fill,
                requested.notional * fee_rate,
            ))
        fees = sum((leg.entry_fee for leg in legs), ZERO)
        trade_id = str(uuid5(
            NAMESPACE_URL,
            f"{portfolio.strategy}:{market.timestamp.isoformat()}:{intent.reason}",
        ))
        trade = Trade(
            trade_id,
            portfolio.strategy,
            market.timestamp,
            intent.reason,
            tuple(legs),
            fees=fees,
            net_pnl=-fees,
        )
        updated = replace(
            portfolio,
            balance=portfolio.balance - fees,
            equity=portfolio.equity - fees,
            trades=portfolio.trades + (trade,),
        )
        return updated, trade

    def _close(
        self, portfolio: Portfolio, intent: OrderIntent, market: MarketSnapshot
    ) -> tuple[Portfolio, Trade]:
        open_trade = portfolio.open_trade
        if not open_trade:
            raise ValueError("portfolio has no open trade")
        gross = ZERO
        exit_fees = ZERO
        closed_legs = []
        for leg in open_trade.legs:
            quote = market.quotes[leg.instrument]
            market_price = quote.bid if leg.side is Side.LONG else quote.ask
            fill = market_price * (ONE - self.slippage * leg.side.sign)
            gross += leg.quantity * (fill - leg.entry_price) * leg.side.sign
            exit_fee = leg.quantity * fill * self.taker_fee
            exit_fees += exit_fee
            closed_legs.append(replace(leg, exit_price=fill, exit_fee=exit_fee))
        fees = open_trade.fees + exit_fees
        closed = replace(
            open_trade,
            legs=tuple(closed_legs),
            status="CLOSED",
            closed_at=market.timestamp,
            exit_reason=intent.reason,
            gross_pnl=gross,
            fees=fees,
            net_pnl=gross + open_trade.funding - fees,
        )
        trades = tuple(closed if trade.id == closed.id else trade for trade in portfolio.trades)
        balance = portfolio.balance + gross - exit_fees
        return replace(
            portfolio,
            balance=balance,
            equity=balance,
            peak_equity=max(portfolio.peak_equity, balance),
            trades=trades,
        ), closed
