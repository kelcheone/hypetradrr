from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import asyncpg

from .config import Settings
from .engine import (
    Direction, Price, ReversalStrategy, Signal, account_status, eligibility_reason,
    exit_fill, exit_reason, gross_pnl, plan_fill,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id text PRIMARY KEY,
    name text NOT NULL,
    started_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    completed_at timestamptz,
    status text NOT NULL CHECK (status IN ('RUNNING','COMPLETED','INVALIDATED')),
    strategy_version text NOT NULL,
    git_commit text NOT NULL,
    configuration jsonb NOT NULL,
    strategy_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_price numeric,
    last_tick_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounts (
    id text PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES experiments(id),
    name text NOT NULL,
    sizing_group text NOT NULL CHECK (sizing_group IN ('RISK_NORMALIZED','FIXED_MARGIN')),
    leverage integer NOT NULL,
    starting_balance numeric NOT NULL,
    balance numeric NOT NULL,
    equity numeric NOT NULL,
    peak_equity numeric NOT NULL,
    max_drawdown numeric NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'ACTIVE',
    daily_date date NOT NULL,
    daily_start_equity numeric NOT NULL,
    daily_trades integer NOT NULL DEFAULT 0,
    cooldown_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(experiment_id, sizing_group, leverage)
);

CREATE TABLE IF NOT EXISTS signals (
    id text PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES experiments(id),
    timestamp timestamptz NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('LONG','SHORT')),
    reference_price numeric NOT NULL,
    trigger_price numeric NOT NULL,
    confirmation_price numeric NOT NULL,
    rolling_high numeric NOT NULL,
    rolling_low numeric NOT NULL,
    initial_move numeric NOT NULL,
    confirmation_move numeric NOT NULL,
    reference_timestamp timestamptz NOT NULL,
    trigger_timestamp timestamptz NOT NULL,
    lookback_start timestamptz NOT NULL,
    strategy text NOT NULL,
    strategy_version text NOT NULL,
    reason text NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id text PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES experiments(id),
    account_id text NOT NULL REFERENCES accounts(id),
    signal_id text NOT NULL REFERENCES signals(id),
    symbol text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('LONG','SHORT')),
    status text NOT NULL CHECK (status IN ('OPEN','CLOSED','LIQUIDATED')),
    entry_timestamp timestamptz NOT NULL,
    entry_market_price numeric NOT NULL,
    entry_price numeric NOT NULL,
    quantity numeric NOT NULL,
    notional numeric NOT NULL,
    margin numeric NOT NULL,
    leverage integer NOT NULL,
    take_profit_price numeric NOT NULL,
    stop_loss_price numeric NOT NULL,
    liquidation_price numeric NOT NULL,
    exit_timestamp timestamptz,
    exit_market_price numeric,
    exit_price numeric,
    exit_reason text,
    btc_move numeric,
    gross_pnl numeric,
    net_pnl numeric,
    entry_fee numeric NOT NULL,
    exit_fee numeric NOT NULL DEFAULT 0,
    funding numeric NOT NULL DEFAULT 0,
    slippage_cost numeric NOT NULL DEFAULT 0,
    liquidation_warning numeric NOT NULL DEFAULT 0,
    balance_after numeric,
    UNIQUE(account_id, signal_id)
);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS liquidation_warning numeric NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS one_open_position_per_account
    ON positions(account_id) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS signal_decisions (
    signal_id text NOT NULL REFERENCES signals(id),
    account_id text NOT NULL REFERENCES accounts(id),
    decision text NOT NULL,
    reason text NOT NULL,
    PRIMARY KEY(signal_id, account_id)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    account_id text NOT NULL REFERENCES accounts(id),
    timestamp timestamptz NOT NULL,
    balance numeric NOT NULL,
    equity numeric NOT NULL,
    unrealized_pnl numeric NOT NULL,
    drawdown numeric NOT NULL,
    PRIMARY KEY(account_id, timestamp)
);

CREATE TABLE IF NOT EXISTS market_prices (
    experiment_id text NOT NULL REFERENCES experiments(id),
    timestamp timestamptz NOT NULL,
    symbol text NOT NULL,
    bid numeric NOT NULL,
    ask numeric NOT NULL,
    mid numeric NOT NULL,
    source text NOT NULL,
    PRIMARY KEY(experiment_id, timestamp)
);

CREATE TABLE IF NOT EXISTS system_events (
    id bigserial PRIMARY KEY,
    experiment_id text REFERENCES experiments(id),
    timestamp timestamptz NOT NULL DEFAULT now(),
    severity text NOT NULL,
    event_type text NOT NULL,
    message text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS positions_experiment_status ON positions(experiment_id, status);
CREATE INDEX IF NOT EXISTS positions_exit_time ON positions(experiment_id, exit_timestamp);
CREATE INDEX IF NOT EXISTS signals_experiment_time ON signals(experiment_id, timestamp);
CREATE INDEX IF NOT EXISTS prices_experiment_time ON market_prices(experiment_id, timestamp);
"""


def _record(row: asyncpg.Record | None) -> dict[str, Any] | None:
    if not row:
        return None
    result = dict(row)
    for key in ("configuration", "strategy_state", "metadata"):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    return result


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.settings.database_url, min_size=1, max_size=6)
        async with self.pool.acquire() as connection:
            await connection.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("database is not connected")
        return self.pool

    async def event(
        self,
        experiment_id: str | None,
        event_type: str,
        message: str,
        severity: str = "INFO",
        metadata: dict[str, object] | None = None,
    ) -> None:
        await self._pool().execute(
            "INSERT INTO system_events(experiment_id,severity,event_type,message,metadata) VALUES($1,$2,$3,$4,$5::jsonb)",
            experiment_id, severity, event_type, message, json.dumps(metadata or {}),
        )

    async def create_experiment(self, margin_metadata: dict[str, object]) -> dict[str, Any]:
        existing = await self._pool().fetchrow(
            "SELECT id FROM experiments WHERE status='RUNNING' ORDER BY created_at DESC LIMIT 1"
        )
        if existing:
            raise RuntimeError(f"experiment {existing['id']} is already running")
        experiment_id = str(uuid4())
        now = datetime.now(timezone.utc).replace(microsecond=0)
        ends_at = now + timedelta(days=self.settings.duration_days)
        git_commit = os.getenv("GIT_COMMIT")
        if not git_commit:
            try:
                git_commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
                ).stdout.strip()
            except (subprocess.CalledProcessError, FileNotFoundError):
                git_commit = "UNCOMMITTED"
        if git_commit in {"UNCOMMITTED", "replace-with-output-of-git-rev-parse-HEAD"}:
            raise RuntimeError("Set GIT_COMMIT to the deployed commit before starting the official experiment")
        configuration = self.settings.snapshot() | {"hyperliquid_margin": margin_metadata}
        async with self._pool().acquire() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO experiments
                (id,name,started_at,ends_at,status,strategy_version,git_commit,configuration)
                VALUES($1,$2,$3,$4,'RUNNING',$5,$6,$7::jsonb)""",
                experiment_id, "BTC 7-day leverage experiment", now, ends_at,
                "btc-reversal-bidirectional-v1", git_commit, json.dumps(configuration),
            )
            for group in ("RISK_NORMALIZED", "FIXED_MARGIN"):
                for leverage in self.settings.leverages:
                    account_id = str(uuid4())
                    await connection.execute(
                        """INSERT INTO accounts
                        (id,experiment_id,name,sizing_group,leverage,starting_balance,balance,equity,
                         peak_equity,daily_date,daily_start_equity)
                        VALUES($1,$2,$3,$4,$5,$6,$6,$6,$6,$7,$6)""",
                        account_id, experiment_id, f"{group}-{leverage}X", group, leverage,
                        self.settings.starting_balance, now.date(),
                    )
            await connection.execute(
                """INSERT INTO system_events(experiment_id,severity,event_type,message,metadata)
                VALUES($1,'INFO','EXPERIMENT_STARTED','Official paper experiment started',$2::jsonb)""",
                experiment_id, json.dumps({"ends_at": ends_at.isoformat()}),
            )
        result = await self.experiment(experiment_id)
        assert result
        return result

    async def experiment(self, experiment_id: str | None = None) -> dict[str, Any] | None:
        if experiment_id:
            row = await self._pool().fetchrow("SELECT * FROM experiments WHERE id=$1", experiment_id)
        else:
            row = await self._pool().fetchrow("SELECT * FROM experiments ORDER BY created_at DESC LIMIT 1")
        return _record(row)

    async def running_experiment(self) -> dict[str, Any] | None:
        return _record(await self._pool().fetchrow(
            "SELECT * FROM experiments WHERE status='RUNNING' ORDER BY created_at DESC LIMIT 1"
        ))

    async def restore_strategy(self, strategy: ReversalStrategy, experiment: dict[str, Any]) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.settings.lookback_minutes)
        rows = await self._pool().fetch(
            "SELECT timestamp,mid FROM market_prices WHERE experiment_id=$1 AND timestamp >= $2 ORDER BY timestamp",
            experiment["id"], cutoff,
        )
        strategy.restore(experiment.get("strategy_state") or {}, ((r["timestamp"], r["mid"]) for r in rows))

    async def record_price(self, experiment_id: str, tick: Price, state: dict[str, object]) -> None:
        async with self._pool().acquire() as connection, connection.transaction():
            await connection.execute(
                """INSERT INTO market_prices(experiment_id,timestamp,symbol,bid,ask,mid,source)
                VALUES($1,$2,$3,$4,$5,$6,'hyperliquid-bbo') ON CONFLICT DO NOTHING""",
                experiment_id, tick.timestamp, self.settings.symbol, tick.bid, tick.ask, tick.mid,
            )
            await connection.execute(
                "UPDATE experiments SET last_price=$2,last_tick_at=$3,strategy_state=$4::jsonb WHERE id=$1",
                experiment_id, tick.mid, tick.timestamp, json.dumps(state),
            )

    async def process_signal(
        self, experiment: dict[str, Any], signal: Signal, market_price: Decimal, maintenance_rate: Decimal
    ) -> int:
        opened = 0
        now = signal.timestamp
        async with self._pool().acquire() as connection, connection.transaction():
            inserted = await connection.execute(
                """INSERT INTO signals
                (id,experiment_id,timestamp,symbol,direction,reference_price,trigger_price,
                 confirmation_price,rolling_high,rolling_low,initial_move,confirmation_move,
                 reference_timestamp,trigger_timestamp,lookback_start,strategy,strategy_version,reason)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
                ON CONFLICT DO NOTHING""",
                signal.id, experiment["id"], signal.timestamp, self.settings.symbol, signal.direction.value,
                signal.reference_price, signal.trigger_price, signal.confirmation_price,
                signal.rolling_high, signal.rolling_low, signal.initial_move, signal.confirmation_move,
                signal.reference_timestamp, signal.trigger_timestamp, signal.lookback_start,
                "rolling-reversal", "btc-reversal-bidirectional-v1", signal.reason,
            )
            if inserted == "INSERT 0 0":
                return 0
            accounts = await connection.fetch(
                "SELECT * FROM accounts WHERE experiment_id=$1 ORDER BY sizing_group,leverage FOR UPDATE",
                experiment["id"],
            )
            for account in accounts:
                reason = await self._eligibility(connection, account, now)
                if reason:
                    await self._decision(connection, signal.id, account["id"], "SKIPPED", reason)
                    continue
                plan = plan_fill(
                    equity=account["equity"], leverage=account["leverage"],
                    group=account["sizing_group"], market_price=market_price,
                    direction=signal.direction, settings=self.settings,
                    maintenance_rate=maintenance_rate,
                )
                if not plan:
                    await self._decision(connection, signal.id, account["id"], "SKIPPED", "INSUFFICIENT_MARGIN")
                    continue
                position_id = str(uuid4())
                await connection.execute(
                    """INSERT INTO positions
                    (id,experiment_id,account_id,signal_id,symbol,direction,status,entry_timestamp,
                     entry_market_price,entry_price,quantity,notional,margin,leverage,take_profit_price,
                     stop_loss_price,liquidation_price,entry_fee,slippage_cost)
                    VALUES($1,$2,$3,$4,$5,$6,'OPEN',$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)""",
                    position_id, experiment["id"], account["id"], signal.id, self.settings.symbol,
                    signal.direction.value, now, market_price, plan.entry_price, plan.quantity,
                    plan.notional, plan.margin, account["leverage"], plan.take_profit,
                    plan.stop_loss, plan.liquidation, plan.entry_fee, plan.slippage_cost,
                )
                await connection.execute(
                    """UPDATE accounts SET balance=balance-$2,equity=equity-$2,daily_trades=daily_trades+1
                    WHERE id=$1""", account["id"], plan.entry_fee,
                )
                await self._decision(connection, signal.id, account["id"], "OPENED", "ELIGIBLE")
                opened += 1
        return opened

    async def _eligibility(self, connection: asyncpg.Connection, account: asyncpg.Record, now: datetime) -> str | None:
        if account["daily_date"] != now.date():
            status = "ACTIVE" if account["status"] == "DAILY_STOPPED" else account["status"]
            await connection.execute(
                """UPDATE accounts SET daily_date=$2,daily_start_equity=equity,daily_trades=0,status=$3
                WHERE id=$1""", account["id"], now.date(), status,
            )
            account = await connection.fetchrow("SELECT * FROM accounts WHERE id=$1", account["id"])
        has_position = await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM positions WHERE account_id=$1 AND status='OPEN')", account["id"]
        )
        return eligibility_reason(
            status=account["status"], has_position=has_position,
            daily_trades=account["daily_trades"], cooldown_until=account["cooldown_until"],
            now=now, max_trades=self.settings.max_trades_per_day,
        )

    async def _decision(
        self, connection: asyncpg.Connection, signal_id: str, account_id: str, decision: str, reason: str
    ) -> None:
        await connection.execute(
            "INSERT INTO signal_decisions(signal_id,account_id,decision,reason) VALUES($1,$2,$3,$4)",
            signal_id, account_id, decision, reason,
        )

    async def mark_positions(self, experiment_id: str, tick: Price, snapshot: bool = False) -> int:
        closed = 0
        async with self._pool().acquire() as connection, connection.transaction():
            positions = await connection.fetch(
                """SELECT p.*,a.balance,a.starting_balance,a.peak_equity,a.daily_start_equity,
                a.daily_date,a.status AS account_status FROM positions p JOIN accounts a ON a.id=p.account_id
                WHERE p.experiment_id=$1 AND p.status='OPEN' FOR UPDATE OF p,a""",
                experiment_id,
            )
            for position in positions:
                direction = Direction(position["direction"])
                unrealized = gross_pnl(position["quantity"], position["entry_price"], tick.mid, direction)
                equity = position["balance"] + unrealized
                peak = max(position["peak_equity"], equity)
                drawdown = max(Decimal(0), (peak - equity) / peak) if peak else Decimal(0)
                await connection.execute(
                    """UPDATE accounts SET equity=$2,peak_equity=$3,max_drawdown=GREATEST(max_drawdown,$4)
                    WHERE id=$1""", position["account_id"], equity, peak, drawdown,
                )
                adverse = (
                    position["entry_price"] - tick.mid if direction is Direction.LONG
                    else tick.mid - position["entry_price"]
                )
                liquidation_span = abs(position["entry_price"] - position["liquidation_price"])
                proximity = max(Decimal(0), adverse / liquidation_span) if liquidation_span else Decimal(1)
                warning = max(
                    (level for level in map(Decimal, ("0.75", "0.90", "0.95")) if proximity >= level),
                    default=Decimal(0),
                )
                if warning > position["liquidation_warning"]:
                    await connection.execute(
                        "UPDATE positions SET liquidation_warning=$2 WHERE id=$1", position["id"], warning
                    )
                    await connection.execute(
                        """INSERT INTO system_events(experiment_id,severity,event_type,message,metadata)
                        VALUES($1,'WARNING','NEAR_LIQUIDATION',$2,$3::jsonb)""",
                        position["experiment_id"],
                        f"{position['direction']} {position['leverage']}x position reached {warning * 100:.0f}% of liquidation distance",
                        json.dumps({"position_id": position["id"], "proximity": str(proximity)}),
                    )
                if snapshot:
                    await connection.execute(
                        """INSERT INTO equity_snapshots(account_id,timestamp,balance,equity,unrealized_pnl,drawdown)
                        VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING""",
                        position["account_id"], tick.timestamp.replace(second=0, microsecond=0),
                        position["balance"], equity, unrealized, drawdown,
                    )
                reason = exit_reason(
                    tick.mid, direction, position["take_profit_price"], position["stop_loss_price"],
                    position["liquidation_price"],
                )
                if reason:
                    await self._close_position(connection, position, tick, reason)
                    closed += 1
            if snapshot:
                await connection.execute(
                    """INSERT INTO equity_snapshots(account_id,timestamp,balance,equity,unrealized_pnl,drawdown)
                    SELECT id,$2,balance,equity,0,CASE WHEN peak_equity=0 THEN 0 ELSE (peak_equity-equity)/peak_equity END
                    FROM accounts a WHERE experiment_id=$1 AND NOT EXISTS
                    (SELECT 1 FROM equity_snapshots e WHERE e.account_id=a.id AND e.timestamp=$2)
                    ON CONFLICT DO NOTHING""",
                    experiment_id, tick.timestamp.replace(second=0, microsecond=0),
                )
        return closed

    async def roll_day(self, experiment_id: str, day: date) -> None:
        await self._pool().execute(
            """UPDATE accounts SET daily_date=$2,daily_start_equity=equity,daily_trades=0,
            status=CASE WHEN status='DAILY_STOPPED' THEN 'ACTIVE' ELSE status END
            WHERE experiment_id=$1 AND daily_date<>$2""",
            experiment_id, day,
        )

    async def _close_position(
        self, connection: asyncpg.Connection, position: asyncpg.Record, tick: Price, reason: str
    ) -> None:
        direction = Direction(position["direction"])
        fill = exit_fill(tick.mid, direction, self.settings.slippage)
        gross = gross_pnl(position["quantity"], position["entry_price"], fill, direction)
        exit_notional = position["quantity"] * fill
        exit_fee = exit_notional * self.settings.taker_fee
        net = gross - position["entry_fee"] - exit_fee
        balance = position["balance"] + gross - exit_fee
        slippage_cost = position["slippage_cost"] + position["quantity"] * abs(fill - tick.mid)
        peak = max(position["peak_equity"], balance)
        drawdown = max(Decimal(0), (peak - balance) / peak) if peak else Decimal(0)
        status = account_status(
            balance=balance, starting_balance=position["starting_balance"],
            daily_start_equity=position["daily_start_equity"], drawdown=drawdown,
            liquidation=reason == "LIQUIDATED", settings=self.settings,
        )
        await connection.execute(
            """UPDATE positions SET status=$2,exit_timestamp=$3,exit_market_price=$4,exit_price=$5,
            exit_reason=$6,btc_move=($5-entry_price)/entry_price,gross_pnl=$7,net_pnl=$8,
            exit_fee=$9,slippage_cost=$10,balance_after=$11 WHERE id=$1""",
            position["id"], "LIQUIDATED" if reason == "LIQUIDATED" else "CLOSED", tick.timestamp,
            tick.mid, fill, reason, gross, net, exit_fee, slippage_cost, balance,
        )
        await connection.execute(
            """UPDATE accounts SET balance=$2,equity=$2,peak_equity=$3,
            max_drawdown=GREATEST(max_drawdown,$4),status=$5,cooldown_until=$6 WHERE id=$1""",
            position["account_id"], balance, peak, drawdown, status,
            tick.timestamp + timedelta(seconds=self.settings.cooldown_seconds),
        )
        await connection.execute(
            """INSERT INTO system_events(experiment_id,severity,event_type,message,metadata)
            VALUES($1,$2,$3,$4,$5::jsonb)""",
            position["experiment_id"], "WARNING" if reason == "LIQUIDATED" else "INFO",
            "POSITION_LIQUIDATED" if reason == "LIQUIDATED" else "POSITION_CLOSED",
            f"{position['direction']} {position['leverage']}x position closed: {reason}",
            json.dumps({"position_id": position["id"], "net_pnl": str(net), "account_status": status}),
        )

    async def force_complete(self, experiment: dict[str, Any], tick: Price) -> None:
        async with self._pool().acquire() as connection, connection.transaction():
            positions = await connection.fetch(
                """SELECT p.*,a.balance,a.starting_balance,a.peak_equity,a.daily_start_equity
                FROM positions p JOIN accounts a ON a.id=p.account_id
                WHERE p.experiment_id=$1 AND p.status='OPEN' FOR UPDATE OF p,a""", experiment["id"],
            )
            for position in positions:
                await self._close_position(connection, position, tick, "EXPERIMENT_END")
            await connection.execute(
                "UPDATE experiments SET status='COMPLETED',completed_at=$2 WHERE id=$1",
                experiment["id"], tick.timestamp,
            )
        await self.event(experiment["id"], "EXPERIMENT_COMPLETED", "Experiment completed and positions closed")

    async def dashboard_rows(
        self, experiment_id: str, include_equity: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        accounts = [dict(row) for row in await self._pool().fetch(
            "SELECT * FROM accounts WHERE experiment_id=$1 ORDER BY sizing_group DESC,leverage", experiment_id
        )]
        positions = [dict(row) for row in await self._pool().fetch(
            """SELECT p.*,a.name AS account_name,a.sizing_group FROM positions p
            JOIN accounts a ON a.id=p.account_id WHERE p.experiment_id=$1 ORDER BY entry_timestamp DESC""",
            experiment_id,
        )]
        signals = [dict(row) for row in await self._pool().fetch(
            "SELECT * FROM signals WHERE experiment_id=$1 ORDER BY timestamp DESC", experiment_id
        )]
        equity = await self.equity_rows(experiment_id, sampled=False) if include_equity else []
        events = [_record(row) for row in await self._pool().fetch(
            "SELECT * FROM system_events WHERE experiment_id=$1 ORDER BY timestamp DESC LIMIT 50", experiment_id
        )]
        return {"accounts": accounts, "positions": positions, "signals": signals, "equity": equity, "events": events}

    async def equity_rows(self, experiment_id: str, sampled: bool = True) -> list[dict[str, Any]]:
        sample_clause = "AND (EXTRACT(MINUTE FROM e.timestamp)::int % 15 = 0 OR e.timestamp >= now() - interval '5 minutes')" if sampled else ""
        return [dict(row) for row in await self._pool().fetch(
            f"""SELECT e.*,a.name AS account_name,a.sizing_group,a.leverage FROM equity_snapshots e
            JOIN accounts a ON a.id=e.account_id WHERE a.experiment_id=$1 {sample_clause}
            ORDER BY timestamp""", experiment_id
        )]

    async def export_rows(self, experiment_id: str, kind: str) -> list[dict[str, Any]]:
        queries = {
            "trades": """SELECT a.name AS account,a.sizing_group,p.* FROM positions p JOIN accounts a ON a.id=p.account_id WHERE p.experiment_id=$1 ORDER BY entry_timestamp""",
            "signals": "SELECT * FROM signals WHERE experiment_id=$1 ORDER BY timestamp",
            "accounts": "SELECT * FROM accounts WHERE experiment_id=$1 ORDER BY sizing_group,leverage",
            "equity": """SELECT a.name AS account,e.* FROM equity_snapshots e JOIN accounts a ON a.id=e.account_id WHERE a.experiment_id=$1 ORDER BY timestamp,a.name""",
            "prices": "SELECT * FROM market_prices WHERE experiment_id=$1 ORDER BY timestamp",
        }
        if kind not in queries:
            raise KeyError(kind)
        return [dict(row) for row in await self._pool().fetch(queries[kind], experiment_id)]
