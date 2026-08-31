from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from decimal import Decimal

from .config import _bool, _decimal


@dataclass(frozen=True, slots=True)
class LabSettings:
    database_url: str
    paper_only: bool
    starting_balance: Decimal
    duration_days: int
    perp_taker_fee: Decimal
    perp_maker_fee: Decimal
    spot_taker_fee: Decimal
    spot_maker_fee: Decimal
    slippage: Decimal
    max_gross: Decimal
    daily_loss: Decimal
    max_drawdown: Decimal
    stale_seconds: int
    reports_dir: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> LabSettings:
        settings = cls(
            database_url=os.getenv("DATABASE_URL", "postgresql://paper:paper@localhost:5432/paper"),
            paper_only=_bool("PAPER_ONLY", "true"),
            starting_balance=_decimal("LAB_STARTING_BALANCE", "10000"),
            duration_days=int(os.getenv("LAB_DURATION_DAYS", "30")),
            perp_taker_fee=_decimal("PERP_TAKER_FEE_BPS", "4.5") / 10_000,
            perp_maker_fee=_decimal("PERP_MAKER_FEE_BPS", "1.5") / 10_000,
            spot_taker_fee=_decimal("SPOT_TAKER_FEE_BPS", "7") / 10_000,
            spot_maker_fee=_decimal("SPOT_MAKER_FEE_BPS", "4") / 10_000,
            slippage=_decimal("LAB_SLIPPAGE_BPS", "1") / 10_000,
            max_gross=_decimal("LAB_MAX_GROSS_MULTIPLE", "2"),
            daily_loss=_decimal("LAB_MAX_DAILY_LOSS_PCT", "2") / 100,
            max_drawdown=_decimal("LAB_MAX_DRAWDOWN_PCT", "15") / 100,
            stale_seconds=int(os.getenv("MARKET_DATA_STALE_SECONDS", "10")),
            reports_dir=os.getenv("REPORTS_DIR", "/app/reports"),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.paper_only:
            raise ValueError("PAPER_ONLY must be true; the lab cannot place real orders")
        if self.duration_days < 1:
            raise ValueError("LAB_DURATION_DAYS must be positive")
        if min(
            self.starting_balance, self.perp_taker_fee, self.perp_maker_fee,
            self.spot_taker_fee, self.spot_maker_fee, self.max_gross,
            self.daily_loss, self.max_drawdown,
        ) <= 0 or self.slippage < 0:
            raise ValueError("lab balances, costs, exposure, and risk limits must be valid")

    @property
    def carry_roundtrip_cost(self) -> Decimal:
        return 2 * (self.spot_taker_fee + self.perp_taker_fee) + 4 * self.slippage

    def snapshot(self) -> dict[str, object]:
        values = asdict(self)
        for key in ("database_url", "host", "port"):
            values.pop(key)
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}
