from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from .market import Candle
from .portfolio import Action, MarketSnapshot, OrderIntent, PaperBroker, PendingOrder, Portfolio, RiskPolicy
from .strategies import Strategy, StrategyContext


@dataclass(frozen=True, slots=True)
class LabEvent:
    timestamp: datetime
    strategy: str
    kind: str
    message: str
    trade_id: str | None = None


class Lab:
    """Run every strategy against one ordered market-observation stream."""

    def __init__(
        self,
        strategies: tuple[Strategy, ...],
        *,
        starting_balance: Decimal,
        broker: PaperBroker,
        risk: RiskPolicy,
    ) -> None:
        keys = [strategy.key for strategy in strategies]
        if len(keys) != len(set(keys)):
            raise ValueError("strategy keys must be unique")
        self.strategies = strategies
        self.broker = broker
        self.risk = risk
        self.portfolios = {
            strategy.key: Portfolio.create(strategy.key, starting_balance)
            for strategy in strategies
        }
        self.events: list[LabEvent] = []

    def step(
        self,
        market: MarketSnapshot,
        history: dict[tuple[object, int], tuple[Candle, ...]],
        funding_history: dict[object, tuple[Decimal, ...]] | None = None,
        evaluate_keys: set[str] | None = None,
    ) -> None:
        for strategy in self.strategies:
            risk_managed = getattr(strategy, "risk_managed", True)
            portfolio = self.portfolios[strategy.key]
            if portfolio.open_trade:
                portfolio = self.broker.mark(portfolio, market)
                if market.funding:
                    portfolio, payment = self.broker.settle_funding(portfolio, market)
                    if payment:
                        self.events.append(LabEvent(
                            market.timestamp, strategy.key, "FUNDING", str(payment),
                            portfolio.open_trade.id if portfolio.open_trade else None,
                        ))
            if risk_managed:
                portfolio = self.risk.refresh(portfolio, market.timestamp)
            if risk_managed and portfolio.status != "ACTIVE":
                if portfolio.pending_order:
                    portfolio = replace(portfolio, pending_order=None)
                    self.events.append(LabEvent(
                        market.timestamp, strategy.key, "ORDER_CANCELLED", portfolio.status,
                    ))
                if portfolio.open_trade:
                    portfolio, trade = self.broker.execute(
                        portfolio, OrderIntent(Action.CLOSE, reason=portfolio.status), market,
                    )
                    assert trade is not None
                    self.events.append(LabEvent(
                        market.timestamp, strategy.key, "RISK_EXIT", portfolio.status, trade.id,
                    ))
                self.portfolios[strategy.key] = portfolio
                continue
            pending = portfolio.pending_order
            if pending and market.timestamp < pending.expires_at:
                portfolio, trade = self.broker.execute(portfolio, pending.intent, market)
                if trade:
                    portfolio = replace(portfolio, pending_order=None)
                    self.events.append(LabEvent(
                        market.timestamp, strategy.key, "TRADE_OPENED",
                        pending.intent.reason, trade.id,
                    ))
                self.portfolios[strategy.key] = portfolio
                continue
            if pending:
                portfolio = replace(portfolio, pending_order=None)
                self.events.append(LabEvent(
                    market.timestamp, strategy.key, "ORDER_EXPIRED", pending.intent.reason,
                ))
            if evaluate_keys is not None and strategy.key not in evaluate_keys:
                self.portfolios[strategy.key] = portfolio
                continue
            context = StrategyContext(market, history, portfolio, funding_history)
            intent = strategy.evaluate(context)
            if not intent:
                self.portfolios[strategy.key] = portfolio
                continue
            rejection = self.risk.rejection(portfolio, intent) if risk_managed else None
            if rejection:
                portfolio = replace(portfolio, last_decision_at=market.timestamp)
                self.events.append(LabEvent(
                    market.timestamp, strategy.key, "RISK_REJECTED", rejection,
                ))
                self.portfolios[strategy.key] = portfolio
                continue
            portfolio = replace(portfolio, last_decision_at=market.timestamp)
            portfolio, trade = self.broker.execute(portfolio, intent, market)
            if not trade:
                portfolio = replace(portfolio, pending_order=PendingOrder(
                    intent, market.timestamp, market.timestamp + timedelta(seconds=60),
                ))
            if risk_managed:
                portfolio = self.risk.refresh(portfolio, market.timestamp)
            self.portfolios[strategy.key] = portfolio
            self.events.append(LabEvent(
                market.timestamp,
                strategy.key,
                "TRADE_OPENED" if intent.action is Action.OPEN and trade else
                "TRADE_CLOSED" if trade else "ORDER_UNFILLED",
                intent.reason,
                trade.id if trade else None,
            ))
