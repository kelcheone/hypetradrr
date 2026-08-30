from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .market import Candle
from .portfolio import MarketSnapshot, PaperBroker, Portfolio, RiskPolicy
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
    ) -> None:
        for strategy in self.strategies:
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
            context = StrategyContext(market, history, portfolio, funding_history)
            intent = strategy.evaluate(context)
            if not intent:
                self.portfolios[strategy.key] = portfolio
                continue
            rejection = self.risk.rejection(portfolio, intent)
            if rejection:
                self.events.append(LabEvent(
                    market.timestamp, strategy.key, "RISK_REJECTED", rejection,
                ))
                self.portfolios[strategy.key] = portfolio
                continue
            portfolio, trade = self.broker.execute(portfolio, intent, market)
            self.portfolios[strategy.key] = portfolio
            self.events.append(LabEvent(
                market.timestamp,
                strategy.key,
                "TRADE_OPENED" if intent.action == "OPEN" and trade else
                "TRADE_CLOSED" if trade else "ORDER_UNFILLED",
                intent.reason,
                trade.id if trade else None,
            ))
