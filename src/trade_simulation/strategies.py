from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from statistics import median
from typing import Protocol

from .market import Candle
from .portfolio import (
    Action,
    Instrument,
    LegIntent,
    MarketSnapshot,
    OrderIntent,
    OrderType,
    Portfolio,
    Side,
)


@dataclass(frozen=True, slots=True)
class StrategyContext:
    market: MarketSnapshot
    history: dict[tuple[Instrument, int], tuple[Candle, ...]]
    portfolio: Portfolio
    funding_history: dict[Instrument, tuple[Decimal, ...]] | None = None

    def candles(self, instrument: Instrument, minutes: int) -> tuple[Candle, ...]:
        return self.history.get((instrument, minutes), ())


class Strategy(Protocol):
    key: str

    def evaluate(self, context: StrategyContext) -> OrderIntent | None: ...


@dataclass(frozen=True, slots=True)
class CashBenchmark:
    key: str = "cash-benchmark"

    def evaluate(self, context: StrategyContext) -> OrderIntent | None:
        return None


@dataclass(frozen=True, slots=True)
class BuyAndHold:
    instrument: Instrument
    key: str = "btc-benchmark"

    def evaluate(self, context: StrategyContext) -> OrderIntent | None:
        if context.portfolio.trades:
            return None
        return OrderIntent(
            Action.OPEN,
            (LegIntent(self.instrument, Side.LONG, context.portfolio.equity),),
            "buy-and-hold benchmark",
        )


def average_true_range(candles: tuple[Candle, ...], periods: int = 14) -> Decimal | None:
    if len(candles) < periods + 1:
        return None
    ranges = []
    for previous, current in zip(candles[-periods - 1:-1], candles[-periods:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return sum(ranges, Decimal("0")) / periods


@dataclass(frozen=True, slots=True)
class TrendBreakout:
    instrument: Instrument
    lookback: int = 24
    atr_periods: int = 14
    stop_atr: Decimal = Decimal("2")
    risk_fraction: Decimal = Decimal("0.005")
    max_gross: Decimal = Decimal("2")
    key: str = "trend-breakout"

    def evaluate(self, context: StrategyContext) -> OrderIntent | None:
        candles = context.candles(self.instrument, 60)
        if len(candles) < max(self.lookback + 1, self.atr_periods + 1):
            return None
        if context.portfolio.open_trade:
            return self._exit(context, candles)
        current = candles[-1]
        prior = candles[-self.lookback - 1:-1]
        side = (
            Side.LONG if current.close > max(item.high for item in prior)
            else Side.SHORT if current.close < min(item.low for item in prior)
            else None
        )
        atr = average_true_range(candles, self.atr_periods)
        if not side or not atr or atr <= 0:
            return None
        stop_fraction = self.stop_atr * atr / current.close
        notional = min(
            context.portfolio.equity * self.risk_fraction / stop_fraction,
            context.portfolio.equity * self.max_gross,
        )
        return OrderIntent(
            Action.OPEN,
            (LegIntent(self.instrument, side, notional),),
            f"{self.lookback}h breakout with {self.stop_atr} ATR risk",
        )

    def _exit(self, context: StrategyContext, candles: tuple[Candle, ...]) -> OrderIntent | None:
        trade = context.portfolio.open_trade
        assert trade is not None
        leg = trade.legs[0]
        active = tuple(item for item in candles if item.start >= trade.opened_at)
        atr = average_true_range(candles, self.atr_periods)
        if not active or not atr:
            return None
        close = candles[-1].close
        trailing = (
            max(item.high for item in active) - self.stop_atr * atr
            if leg.side is Side.LONG
            else min(item.low for item in active) + self.stop_atr * atr
        )
        hit = close <= trailing if leg.side is Side.LONG else close >= trailing
        return OrderIntent(Action.CLOSE, reason="ATR trailing stop") if hit else None


@dataclass(frozen=True, slots=True)
class VolatilityBreakout:
    instrument: Instrument
    compression_bars: int = 672
    range_bars: int = 12
    atr_periods: int = 14
    stop_atr: Decimal = Decimal("1.5")
    trail_atr: Decimal = Decimal("2.5")
    risk_fraction: Decimal = Decimal("0.005")
    max_gross: Decimal = Decimal("2")
    key: str = "volatility-breakout"

    def evaluate(self, context: StrategyContext) -> OrderIntent | None:
        candles = context.candles(self.instrument, 15)
        needed = max(self.compression_bars + 1, self.atr_periods + 2, self.range_bars + 1)
        if len(candles) < needed:
            return None
        if context.portfolio.open_trade:
            return self._exit(context, candles)
        current = candles[-1]
        prior = candles[-self.compression_bars - 1:-1]
        normalized_atr = []
        for index in range(self.atr_periods + 1, len(prior) + 1):
            sample = prior[:index]
            atr = average_true_range(sample, self.atr_periods)
            if atr:
                normalized_atr.append(atr / sample[-1].close)
        if not normalized_atr:
            return None
        threshold = sorted(normalized_atr)[int((len(normalized_atr) - 1) * Decimal("0.2"))]
        compressed = normalized_atr[-1] <= threshold
        recent = prior[-self.range_bars:]
        volume_confirmed = current.volume > median(item.volume for item in prior[-20:])
        side = (
            Side.LONG if current.close > max(item.high for item in recent)
            else Side.SHORT if current.close < min(item.low for item in recent)
            else None
        )
        atr = average_true_range(candles, self.atr_periods)
        if not compressed or not volume_confirmed or not side or not atr:
            return None
        notional = min(
            context.portfolio.equity * self.risk_fraction / (self.stop_atr * atr / current.close),
            context.portfolio.equity * self.max_gross,
        )
        return OrderIntent(
            Action.OPEN,
            (LegIntent(self.instrument, side, notional),),
            "volatility compression breakout",
        )

    def _exit(self, context: StrategyContext, candles: tuple[Candle, ...]) -> OrderIntent | None:
        trade = context.portfolio.open_trade
        assert trade is not None
        leg = trade.legs[0]
        active = tuple(item for item in candles if item.start >= trade.opened_at)
        atr = average_true_range(candles, self.atr_periods)
        if not active or not atr:
            return None
        close = candles[-1].close
        trailing = (
            max(item.high for item in active) - self.trail_atr * atr
            if leg.side is Side.LONG
            else min(item.low for item in active) + self.trail_atr * atr
        )
        expired = context.market.timestamp - trade.opened_at >= timedelta(hours=12)
        hit = close <= trailing if leg.side is Side.LONG else close >= trailing
        return OrderIntent(Action.CLOSE, reason="volatility exit") if hit or expired else None


def vwap_zscore(candles: tuple[Candle, ...]) -> Decimal | None:
    volume = sum((item.volume for item in candles), Decimal("0"))
    if not candles or volume <= 0:
        return None
    vwap = sum((item.close * item.volume for item in candles), Decimal("0")) / volume
    variance = sum(((item.close - vwap) ** 2 for item in candles), Decimal("0")) / len(candles)
    return (candles[-1].close - vwap) / variance.sqrt() if variance > 0 else Decimal("0")


@dataclass(frozen=True, slots=True)
class MakerMeanReversion:
    instrument: Instrument
    window: int = 60
    entry_z: Decimal = Decimal("2.5")
    exit_z: Decimal = Decimal("0.5")
    risk_fraction: Decimal = Decimal("0.0025")
    adverse_move: Decimal = Decimal("0.005")
    max_hold: timedelta = timedelta(minutes=30)
    key: str = "maker-reversion"

    def evaluate(self, context: StrategyContext) -> OrderIntent | None:
        candles = context.candles(self.instrument, 1)
        if len(candles) < self.window:
            return None
        zscore = vwap_zscore(candles[-self.window:])
        if zscore is None:
            return None
        trade = context.portfolio.open_trade
        if trade:
            leg = trade.legs[0]
            close = candles[-1].close
            reverted = zscore >= -self.exit_z if leg.side is Side.LONG else zscore <= self.exit_z
            adverse = (
                close <= leg.entry_price * (Decimal("1") - self.adverse_move)
                if leg.side is Side.LONG
                else close >= leg.entry_price * (Decimal("1") + self.adverse_move)
            )
            expired = context.market.timestamp - trade.opened_at >= self.max_hold
            return OrderIntent(Action.CLOSE, reason="mean-reversion exit") if reverted or adverse or expired else None
        if abs(zscore) < self.entry_z:
            return None
        side = Side.LONG if zscore < 0 else Side.SHORT
        quote = context.market.quotes[self.instrument]
        limit_price = quote.bid if side is Side.LONG else quote.ask
        notional = context.portfolio.equity * self.risk_fraction / self.adverse_move
        return OrderIntent(
            Action.OPEN,
            (LegIntent(
                self.instrument, side, notional,
                OrderType.POST_ONLY, limit_price,
            ),),
            f"VWAP deviation {zscore:.2f}z",
        )


def _zscore(values: tuple[Decimal, ...]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = sum(values, Decimal("0")) / len(values)
    variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / len(values)
    return (values[-1] - mean) / variance.sqrt() if variance > 0 else Decimal("0")


@dataclass(frozen=True, slots=True)
class PairsMeanReversion:
    btc: Instrument
    eth: Instrument
    regression_bars: int = 720
    zscore_bars: int = 168
    entry_z: Decimal = Decimal("2")
    exit_z: Decimal = Decimal("0.5")
    stop_z: Decimal = Decimal("3.5")
    max_hold: timedelta = timedelta(hours=72)
    leg_fraction: Decimal = Decimal("0.5")
    key: str = "btc-eth-pairs"

    def evaluate(self, context: StrategyContext) -> OrderIntent | None:
        btc = context.candles(self.btc, 60)
        eth = context.candles(self.eth, 60)
        needed = self.regression_bars + 1
        if len(btc) < needed or len(eth) < needed:
            return None
        training_btc = tuple(item.close.ln() for item in btc[-needed:-1])
        training_eth = tuple(item.close.ln() for item in eth[-needed:-1])
        x_mean = sum(training_btc, Decimal("0")) / len(training_btc)
        y_mean = sum(training_eth, Decimal("0")) / len(training_eth)
        variance = sum(((value - x_mean) ** 2 for value in training_btc), Decimal("0"))
        if variance == 0:
            return None
        beta = sum(
            ((x - x_mean) * (y - y_mean) for x, y in zip(training_btc, training_eth)),
            Decimal("0"),
        ) / variance
        intercept = y_mean - beta * x_mean
        paired = zip(btc[-self.zscore_bars:], eth[-self.zscore_bars:])
        residuals = tuple(
            eth_candle.close.ln() - intercept - beta * btc_candle.close.ln()
            for btc_candle, eth_candle in paired
        )
        zscore = _zscore(residuals)
        if zscore is None:
            return None
        trade = context.portfolio.open_trade
        if trade:
            expired = context.market.timestamp - trade.opened_at >= self.max_hold
            if abs(zscore) <= self.exit_z or abs(zscore) >= self.stop_z or expired:
                return OrderIntent(Action.CLOSE, reason=f"pairs spread exit {zscore:.2f}z")
            return None
        if abs(zscore) < self.entry_z:
            return None
        notional = context.portfolio.equity * self.leg_fraction
        legs = (
            (LegIntent(self.btc, Side.LONG, notional), LegIntent(self.eth, Side.SHORT, notional))
            if zscore > 0
            else (LegIntent(self.btc, Side.SHORT, notional), LegIntent(self.eth, Side.LONG, notional))
        )
        return OrderIntent(Action.OPEN, legs, f"BTC/ETH residual {zscore:.2f}z")


@dataclass(frozen=True, slots=True)
class FundingCarry:
    spot: Instrument
    perp: Instrument
    roundtrip_cost: Decimal
    safety_buffer: Decimal = Decimal("0.001")
    allocation: Decimal = Decimal("0.45")
    basis_limit: Decimal = Decimal("0.01")
    projection_hours: int = 24
    key: str = "funding-carry"

    def evaluate(self, context: StrategyContext) -> OrderIntent | None:
        rates = (context.funding_history or {}).get(self.perp, ())
        if len(rates) < 8:
            return None
        hourly = median(rates[-8:])
        projected = hourly * self.projection_hours
        trade = context.portfolio.open_trade
        spot_quote = context.market.quotes[self.spot]
        perp_quote = context.market.quotes[self.perp]
        spot_mid = (spot_quote.bid + spot_quote.ask) / 2
        perp_mid = (perp_quote.bid + perp_quote.ask) / 2
        basis = abs(perp_mid / spot_mid - Decimal("1"))
        if trade:
            uneconomic = projected <= self.roundtrip_cost / 2
            if hourly <= 0 or uneconomic or basis >= self.basis_limit:
                return OrderIntent(Action.CLOSE, reason="funding carry exit")
            return None
        if projected <= self.roundtrip_cost + self.safety_buffer:
            return None
        notional = context.portfolio.equity * self.allocation
        return OrderIntent(
            Action.OPEN,
            (
                LegIntent(self.spot, Side.LONG, notional),
                LegIntent(self.perp, Side.SHORT, notional),
            ),
            f"projected 24h funding {projected:.4%}",
        )
