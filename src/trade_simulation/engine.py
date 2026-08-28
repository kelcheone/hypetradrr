from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

from .config import Settings

ZERO = Decimal("0")
ONE = Decimal("1")


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> Decimal:
        return ONE if self is Direction.LONG else -ONE


@dataclass(frozen=True, slots=True)
class Price:
    timestamp: datetime
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    timestamp: datetime
    direction: Direction
    reference_price: Decimal
    trigger_price: Decimal
    confirmation_price: Decimal
    rolling_high: Decimal
    rolling_low: Decimal
    initial_move: Decimal
    confirmation_move: Decimal
    reference_timestamp: datetime
    trigger_timestamp: datetime
    lookback_start: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class FillPlan:
    entry_price: Decimal
    quantity: Decimal
    notional: Decimal
    margin: Decimal
    take_profit: Decimal
    stop_loss: Decimal
    liquidation: Decimal
    entry_fee: Decimal
    slippage_cost: Decimal


class ReversalStrategy:
    """Symmetric rolling reversal detector with explicit re-arm hysteresis."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.prices: deque[tuple[datetime, Decimal]] = deque()
        self.long_armed: tuple[Decimal, Decimal, datetime, datetime] | None = None
        self.short_armed: tuple[Decimal, Decimal, datetime, datetime] | None = None
        self.long_ready = True
        self.short_ready = True

    def on_price(self, tick: Price) -> list[Signal]:
        price = tick.mid
        cutoff = tick.timestamp - timedelta(minutes=self.settings.lookback_minutes)
        self.prices.append((tick.timestamp, price))
        while self.prices and self.prices[0][0] < cutoff:
            self.prices.popleft()
        if len(self.prices) < 2:
            return []

        high_time, high = max(self.prices, key=lambda item: item[1])
        low_time, low = min(self.prices, key=lambda item: item[1])
        dip = (high - price) / high
        rally = (price - low) / low

        if not self.long_ready and dip < self.settings.dip_trigger:
            self.long_ready = True
        if not self.short_ready and rally < self.settings.rally_trigger:
            self.short_ready = True

        if self.long_ready and self.long_armed is None and dip >= self.settings.dip_trigger:
            self.long_armed = (high, price, high_time, tick.timestamp)
        if self.short_ready and self.short_armed is None and rally >= self.settings.rally_trigger:
            self.short_armed = (low, price, low_time, tick.timestamp)

        signals: list[Signal] = []
        if self.long_armed:
            reference, local_low, reference_time, trigger_time = self.long_armed
            local_low = min(local_low, price)
            self.long_armed = (reference, local_low, reference_time, trigger_time)
            recovery = (price - local_low) / local_low
            if recovery >= self.settings.recovery_trigger and self.settings.enable_longs:
                signals.append(
                    self._signal(
                        tick, Direction.LONG, reference, local_low, high, low,
                        (reference - local_low) / reference, recovery,
                        reference_time, trigger_time,
                    )
                )
                self.long_armed = None
                self.long_ready = False

        if self.short_armed:
            reference, local_high, reference_time, trigger_time = self.short_armed
            local_high = max(local_high, price)
            self.short_armed = (reference, local_high, reference_time, trigger_time)
            reversal = (local_high - price) / local_high
            if reversal >= self.settings.reversal_trigger and self.settings.enable_shorts:
                signals.append(
                    self._signal(
                        tick, Direction.SHORT, reference, local_high, high, low,
                        (local_high - reference) / reference, reversal,
                        reference_time, trigger_time,
                    )
                )
                self.short_armed = None
                self.short_ready = False
        return signals

    def _signal(
        self,
        tick: Price,
        direction: Direction,
        reference: Decimal,
        trigger: Decimal,
        high: Decimal,
        low: Decimal,
        initial: Decimal,
        confirmation: Decimal,
        reference_time: datetime,
        trigger_time: datetime,
    ) -> Signal:
        verb = "declined and recovered" if direction is Direction.LONG else "rallied and reversed"
        return Signal(
            id=str(uuid5(NAMESPACE_URL, f"btc-reversal-bidirectional-v1:{direction}:{trigger_time.isoformat()}")),
            timestamp=tick.timestamp, direction=direction,
            reference_price=reference, trigger_price=trigger, confirmation_price=tick.mid,
            rolling_high=high, rolling_low=low, initial_move=initial,
            confirmation_move=confirmation, reference_timestamp=reference_time,
            trigger_timestamp=trigger_time, lookback_start=self.prices[0][0],
            reason=f"BTC {verb}: {initial * 100:.3f}% / {confirmation * 100:.3f}%",
        )

    def state(self) -> dict[str, object]:
        def armed(value: tuple[Decimal, Decimal, datetime, datetime] | None) -> list[str] | None:
            return [str(item) for item in value] if value else None

        return {
            "long_armed": armed(self.long_armed),
            "short_armed": armed(self.short_armed),
            "long_ready": self.long_ready,
            "short_ready": self.short_ready,
        }

    def restore(self, state: dict[str, object], prices: Iterable[tuple[datetime, Decimal]]) -> None:
        def armed(value: object) -> tuple[Decimal, Decimal, datetime, datetime] | None:
            if not value or not isinstance(value, list):
                return None
            return Decimal(value[0]), Decimal(value[1]), datetime.fromisoformat(value[2]), datetime.fromisoformat(value[3])

        self.prices = deque(prices)
        self.long_armed = armed(state.get("long_armed"))
        self.short_armed = armed(state.get("short_armed"))
        self.long_ready = bool(state.get("long_ready", True))
        self.short_ready = bool(state.get("short_ready", True))


def entry_fill(market_price: Decimal, direction: Direction, slippage: Decimal) -> Decimal:
    return market_price * (ONE + slippage * direction.sign)


def exit_fill(market_price: Decimal, direction: Direction, slippage: Decimal) -> Decimal:
    return market_price * (ONE - slippage * direction.sign)


def gross_pnl(quantity: Decimal, entry: Decimal, exit: Decimal, direction: Direction) -> Decimal:
    return quantity * (exit - entry) * direction.sign


def isolated_liquidation(
    entry: Decimal,
    margin: Decimal,
    quantity: Decimal,
    maintenance_rate: Decimal,
    direction: Direction,
) -> Decimal:
    margin_per_unit = margin / quantity
    if direction is Direction.LONG:
        return max(ZERO, (entry - margin_per_unit) / (ONE - maintenance_rate))
    return (entry + margin_per_unit) / (ONE + maintenance_rate)


def plan_fill(
    *,
    equity: Decimal,
    leverage: int,
    group: str,
    market_price: Decimal,
    direction: Direction,
    settings: Settings,
    maintenance_rate: Decimal,
) -> FillPlan | None:
    entry = entry_fill(market_price, direction, settings.slippage)
    if group == "RISK_NORMALIZED":
        full_stop_cost = settings.stop_loss + 2 * settings.slippage + 2 * settings.taker_fee
        notional = equity * settings.risk_per_trade / full_stop_cost
    else:
        notional = settings.fixed_margin * leverage
    # Keep enough equity for entry fees; lower leverage is otherwise infeasible.
    affordable = equity / (ONE / Decimal(leverage) + settings.taker_fee)
    notional = min(notional, affordable)
    if notional <= ZERO:
        return None
    margin = notional / Decimal(leverage)
    quantity = notional / entry
    entry_fee = notional * settings.taker_fee
    if direction is Direction.LONG:
        take_profit = entry * (ONE + settings.take_profit)
        stop_loss = entry * (ONE - settings.stop_loss)
    else:
        take_profit = entry * (ONE - settings.take_profit)
        stop_loss = entry * (ONE + settings.stop_loss)
    return FillPlan(
        entry_price=entry, quantity=quantity, notional=notional, margin=margin,
        take_profit=take_profit, stop_loss=stop_loss,
        liquidation=isolated_liquidation(entry, margin, quantity, maintenance_rate, direction),
        entry_fee=entry_fee,
        slippage_cost=quantity * abs(entry - market_price),
    )


def exit_reason(
    market_price: Decimal,
    direction: Direction,
    take_profit: Decimal,
    stop_loss: Decimal,
    liquidation: Decimal,
) -> str | None:
    # A gap beyond liquidation is conservatively treated as liquidation before a stop fill.
    if direction is Direction.LONG:
        if market_price <= liquidation:
            return "LIQUIDATED"
        if market_price <= stop_loss:
            return "STOP_LOSS"
        if market_price >= take_profit:
            return "TAKE_PROFIT"
    else:
        if market_price >= liquidation:
            return "LIQUIDATED"
        if market_price >= stop_loss:
            return "STOP_LOSS"
        if market_price <= take_profit:
            return "TAKE_PROFIT"
    return None


def eligibility_reason(
    *, status: str, has_position: bool, daily_trades: int,
    cooldown_until: datetime | None, now: datetime, max_trades: int,
) -> str | None:
    if status != "ACTIVE":
        return status
    if has_position:
        return "POSITION_ALREADY_OPEN"
    if daily_trades >= max_trades:
        return "DAILY_TRADE_LIMIT"
    if cooldown_until and cooldown_until > now:
        return "COOLDOWN"
    return None


def account_status(
    *, balance: Decimal, starting_balance: Decimal, daily_start_equity: Decimal,
    drawdown: Decimal, liquidation: bool, settings: Settings,
) -> str:
    if liquidation and balance <= starting_balance * Decimal("0.1"):
        return "BLOWN_UP"
    if drawdown >= settings.max_drawdown:
        return "DISABLED"
    if balance <= daily_start_equity * (ONE - settings.max_daily_loss):
        return "DAILY_STOPPED"
    return "ACTIVE"
