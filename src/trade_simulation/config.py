from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from decimal import Decimal


def _decimal(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except Exception as exc:
        raise ValueError(f"{name} must be a decimal number") from exc


def _bool(name: str, default: str) -> bool:
    value = os.getenv(name, default).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    paper_only: bool
    symbol: str
    starting_balance: Decimal
    leverages: tuple[int, ...]
    lookback_minutes: int
    dip_trigger: Decimal
    rally_trigger: Decimal
    recovery_trigger: Decimal
    reversal_trigger: Decimal
    take_profit: Decimal
    stop_loss: Decimal
    risk_per_trade: Decimal
    fixed_margin: Decimal
    max_trades_per_day: int
    max_daily_loss: Decimal
    max_drawdown: Decimal
    cooldown_seconds: int
    stale_seconds: int
    slippage: Decimal
    taker_fee: Decimal
    duration_days: int
    enable_longs: bool
    enable_shorts: bool
    funding_mode: str
    reports_dir: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> Settings:
        settings = cls(
            database_url=os.getenv(
                "DATABASE_URL", "postgresql://paper:paper@localhost:5432/paper"
            ),
            paper_only=_bool("PAPER_ONLY", "true"),
            symbol=os.getenv("SYMBOL", "BTC"),
            starting_balance=_decimal("STARTING_BALANCE", "10000"),
            leverages=tuple(
                int(value) for value in os.getenv("LEVERAGES", "5,10,15,20,25,30,35,40").split(",")
            ),
            lookback_minutes=int(os.getenv("LOOKBACK_MINUTES", "5")),
            dip_trigger=_decimal("DIP_TRIGGER_PCT", "0.25") / 100,
            rally_trigger=_decimal("RALLY_TRIGGER_PCT", "0.25") / 100,
            recovery_trigger=_decimal("RECOVERY_TRIGGER_PCT", "0.05") / 100,
            reversal_trigger=_decimal("REVERSAL_TRIGGER_PCT", "0.05") / 100,
            take_profit=_decimal("TAKE_PROFIT_PCT", "0.20") / 100,
            stop_loss=_decimal("STOP_LOSS_PCT", "0.10") / 100,
            risk_per_trade=_decimal("RISK_PER_TRADE_PCT", "1.0") / 100,
            fixed_margin=_decimal("FIXED_MARGIN_PER_TRADE", "2000"),
            max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "10")),
            max_daily_loss=_decimal("MAX_DAILY_LOSS_PCT", "3") / 100,
            max_drawdown=_decimal("MAX_TOTAL_DRAWDOWN_PCT", "30") / 100,
            cooldown_seconds=int(os.getenv("COOLDOWN_SECONDS", "300")),
            stale_seconds=int(os.getenv("MARKET_DATA_STALE_SECONDS", "10")),
            slippage=_decimal("SLIPPAGE_BPS", "1") / 10_000,
            taker_fee=_decimal("TAKER_FEE_BPS", "4.5") / 10_000,
            duration_days=int(os.getenv("EXPERIMENT_DURATION_DAYS", "7")),
            enable_longs=_bool("ENABLE_LONGS", "true"),
            enable_shorts=_bool("ENABLE_SHORTS", "true"),
            funding_mode=os.getenv("FUNDING_MODE", "DISABLED"),
            reports_dir=os.getenv("REPORTS_DIR", "reports"),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.paper_only:
            raise ValueError("PAPER_ONLY must be true; this application cannot trade")
        if self.funding_mode != "DISABLED":
            raise ValueError("Only FUNDING_MODE=DISABLED is supported and reports are labelled accordingly")
        if not self.leverages or any(value < 1 or value > 40 for value in self.leverages):
            raise ValueError("LEVERAGES must contain integers from 1 through 40")
        positive = (
            self.starting_balance,
            self.take_profit,
            self.stop_loss,
            self.risk_per_trade,
            self.fixed_margin,
            self.taker_fee,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("Balances, risk, exits, margin, and fees must be positive")

    def snapshot(self) -> dict[str, object]:
        values = asdict(self)
        values.pop("database_url")
        values.pop("host")
        values.pop("port")
        values["leverages"] = list(self.leverages)
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}
