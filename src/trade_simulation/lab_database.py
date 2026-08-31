from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import asyncpg

from .lab import Lab, LabEvent
from .market import Candle
from .portfolio import (
    Action, Instrument, LegIntent, MarketSnapshot, OrderIntent, OrderType,
    PendingOrder, Portfolio, Side, Trade, TradeLeg,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_experiments (
    id text PRIMARY KEY,
    started_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    completed_at timestamptz,
    status text NOT NULL CHECK (status IN ('RUNNING','COMPLETED','INVALIDATED')),
    git_commit text NOT NULL,
    configuration jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lab_portfolios (
    experiment_id text NOT NULL REFERENCES lab_experiments(id),
    strategy_key text NOT NULL,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY(experiment_id,strategy_key)
);
CREATE TABLE IF NOT EXISTS lab_trades (
    id text PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES lab_experiments(id),
    strategy_key text NOT NULL,
    status text NOT NULL,
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS lab_market_observations (
    experiment_id text NOT NULL REFERENCES lab_experiments(id),
    timestamp timestamptz NOT NULL,
    symbol text NOT NULL,
    instrument_kind text NOT NULL,
    bid numeric NOT NULL,
    ask numeric NOT NULL,
    trade_low numeric,
    trade_high numeric,
    funding_rate numeric,
    PRIMARY KEY(experiment_id,timestamp,symbol,instrument_kind)
);
CREATE TABLE IF NOT EXISTS lab_candles (
    experiment_id text NOT NULL REFERENCES lab_experiments(id),
    symbol text NOT NULL,
    instrument_kind text NOT NULL,
    minutes integer NOT NULL,
    start_at timestamptz NOT NULL,
    open numeric NOT NULL,
    high numeric NOT NULL,
    low numeric NOT NULL,
    close numeric NOT NULL,
    volume numeric NOT NULL,
    PRIMARY KEY(experiment_id,symbol,instrument_kind,minutes,start_at)
);
CREATE TABLE IF NOT EXISTS lab_equity (
    experiment_id text NOT NULL REFERENCES lab_experiments(id),
    strategy_key text NOT NULL,
    timestamp timestamptz NOT NULL,
    balance numeric NOT NULL,
    equity numeric NOT NULL,
    PRIMARY KEY(experiment_id,strategy_key,timestamp)
);
CREATE TABLE IF NOT EXISTS lab_events (
    id bigserial PRIMARY KEY,
    experiment_id text NOT NULL REFERENCES lab_experiments(id),
    timestamp timestamptz NOT NULL,
    strategy_key text NOT NULL,
    kind text NOT NULL,
    message text NOT NULL,
    trade_id text
);
CREATE INDEX IF NOT EXISTS lab_events_time ON lab_events(experiment_id,timestamp);
CREATE INDEX IF NOT EXISTS lab_equity_time ON lab_equity(experiment_id,timestamp);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_lab ON lab_experiments(status) WHERE status='RUNNING';
"""


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def serialize_portfolio(portfolio: Portfolio) -> dict[str, object]:
    return {
        "strategy": portfolio.strategy,
        "starting_balance": str(portfolio.starting_balance),
        "balance": str(portfolio.balance),
        "equity": str(portfolio.equity),
        "peak_equity": str(portfolio.peak_equity),
        "daily_start_equity": str(portfolio.daily_start_equity),
        "status": portfolio.status,
        "daily_date": portfolio.daily_date.isoformat() if portfolio.daily_date else None,
        "last_decision_at": portfolio.last_decision_at.isoformat() if portfolio.last_decision_at else None,
        "pending_order": serialize_pending(portfolio.pending_order),
        "trades": [serialize_trade(trade) for trade in portfolio.trades],
    }


def serialize_pending(pending: PendingOrder | None) -> dict[str, object] | None:
    if not pending:
        return None
    return {
        "submitted_at": pending.submitted_at.isoformat(),
        "expires_at": pending.expires_at.isoformat(),
        "intent": {
            "action": pending.intent.action.value,
            "reason": pending.intent.reason,
            "legs": [{
                "symbol": leg.instrument.symbol,
                "kind": leg.instrument.kind,
                "side": leg.side.value,
                "notional": str(leg.notional),
                "order_type": leg.order_type.value,
                "limit_price": str(leg.limit_price) if leg.limit_price is not None else None,
            } for leg in pending.intent.legs],
        },
    }


def serialize_trade(trade: Trade) -> dict[str, object]:
    return {
        "id": trade.id,
        "strategy": trade.strategy,
        "opened_at": trade.opened_at.isoformat(),
        "entry_reason": trade.entry_reason,
        "status": trade.status,
        "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
        "exit_reason": trade.exit_reason,
        "gross_pnl": str(trade.gross_pnl),
        "fees": str(trade.fees),
        "funding": str(trade.funding),
        "net_pnl": str(trade.net_pnl),
        "legs": [{
            "symbol": leg.instrument.symbol,
            "kind": leg.instrument.kind,
            "side": leg.side.value,
            "quantity": str(leg.quantity),
            "entry_price": str(leg.entry_price),
            "entry_fee": str(leg.entry_fee),
            "exit_price": str(leg.exit_price) if leg.exit_price is not None else None,
            "exit_fee": str(leg.exit_fee),
            "funding_settled_at": leg.funding_settled_at.isoformat() if leg.funding_settled_at else None,
        } for leg in trade.legs],
    }


def deserialize_portfolio(data: dict[str, Any] | str) -> Portfolio:
    if isinstance(data, str):
        data = json.loads(data)
    trades = tuple(Trade(
        item["id"], item["strategy"], datetime.fromisoformat(item["opened_at"]),
        item["entry_reason"],
        tuple(TradeLeg(
            Instrument(leg["symbol"], leg["kind"]), Side(leg["side"]),
            _decimal(leg["quantity"]), _decimal(leg["entry_price"]), _decimal(leg["entry_fee"]),
            _decimal(leg["exit_price"]) if leg["exit_price"] is not None else None,
            _decimal(leg["exit_fee"]),
            datetime.fromisoformat(leg["funding_settled_at"]) if leg.get("funding_settled_at") else None,
        ) for leg in item["legs"]),
        item["status"],
        datetime.fromisoformat(item["closed_at"]) if item["closed_at"] else None,
        item["exit_reason"],
        _decimal(item["gross_pnl"]), _decimal(item["fees"]),
        _decimal(item["funding"]), _decimal(item["net_pnl"]),
    ) for item in data["trades"])
    pending_data = data.get("pending_order")
    pending = None
    if pending_data:
        intent_data = pending_data["intent"]
        pending = PendingOrder(
            OrderIntent(
                Action(intent_data["action"]),
                tuple(LegIntent(
                    Instrument(leg["symbol"], leg["kind"]), Side(leg["side"]),
                    _decimal(leg["notional"]), OrderType(leg["order_type"]),
                    _decimal(leg["limit_price"]) if leg["limit_price"] is not None else None,
                ) for leg in intent_data["legs"]),
                intent_data["reason"],
            ),
            datetime.fromisoformat(pending_data["submitted_at"]),
            datetime.fromisoformat(pending_data["expires_at"]),
        )
    return Portfolio(
        strategy=data["strategy"],
        starting_balance=_decimal(data["starting_balance"]),
        balance=_decimal(data["balance"]),
        equity=_decimal(data["equity"]),
        peak_equity=_decimal(data["peak_equity"]),
        daily_start_equity=_decimal(data["daily_start_equity"]),
        status=data["status"],
        daily_date=datetime.fromisoformat(data["daily_date"]).date() if data.get("daily_date") else None,
        last_decision_at=datetime.fromisoformat(data["last_decision_at"]) if data.get("last_decision_at") else None,
        pending_order=pending,
        trades=trades,
    )


class LabDatabase:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=6)
        await self.pool.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("lab database is not connected")
        return self.pool

    async def create_experiment(
        self,
        lab: Lab,
        *,
        duration_days: int,
        git_commit: str,
        configuration: dict[str, object],
    ) -> dict[str, Any]:
        if await self.running_experiment():
            raise RuntimeError("a strategy experiment is already running")
        if not git_commit or git_commit == "UNCOMMITTED":
            raise RuntimeError("GIT_COMMIT must identify the deployed v2 source")
        experiment_id = str(uuid4())
        started_at = datetime.now(timezone.utc).replace(microsecond=0)
        ends_at = started_at + timedelta(days=duration_days)
        async with self._pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO lab_experiments(id,started_at,ends_at,status,git_commit,configuration) VALUES($1,$2,$3,'RUNNING',$4,$5::jsonb)",
                experiment_id, started_at, ends_at, git_commit, json.dumps(configuration),
            )
            await connection.executemany(
                "INSERT INTO lab_portfolios(experiment_id,strategy_key,state,updated_at) VALUES($1,$2,$3::jsonb,$4)",
                [
                    (experiment_id, key, json.dumps(serialize_portfolio(portfolio)), started_at)
                    for key, portfolio in lab.portfolios.items()
                ],
            )
        return await self.experiment(experiment_id)

    async def experiment(self, experiment_id: str | None = None) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM lab_experiments WHERE id=$1" if experiment_id else
            "SELECT * FROM lab_experiments ORDER BY created_at DESC LIMIT 1",
            *([experiment_id] if experiment_id else []),
        )
        if not row:
            return None
        result = dict(row)
        if isinstance(result.get("configuration"), str):
            result["configuration"] = json.loads(result["configuration"])
        return result

    async def running_experiment(self) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM lab_experiments WHERE status='RUNNING' ORDER BY created_at DESC LIMIT 1"
        )
        if not row:
            return None
        result = dict(row)
        if isinstance(result.get("configuration"), str):
            result["configuration"] = json.loads(result["configuration"])
        return result

    async def load_portfolios(self, experiment_id: str) -> dict[str, Portfolio]:
        rows = await self._pool().fetch(
            "SELECT strategy_key,state FROM lab_portfolios WHERE experiment_id=$1", experiment_id
        )
        return {row["strategy_key"]: deserialize_portfolio(row["state"]) for row in rows}

    async def save_step(
        self,
        experiment_id: str,
        market: MarketSnapshot,
        lab: Lab,
        events: list[LabEvent],
    ) -> None:
        minute = market.timestamp.replace(second=0, microsecond=0)
        async with self._pool().acquire() as connection, connection.transaction():
            for instrument, quote in market.quotes.items():
                trade_range = (market.trade_ranges or {}).get(instrument)
                await connection.execute(
                    """INSERT INTO lab_market_observations
                    (experiment_id,timestamp,symbol,instrument_kind,bid,ask,trade_low,trade_high,funding_rate)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING""",
                    experiment_id, market.timestamp, instrument.symbol, instrument.kind,
                    quote.bid, quote.ask,
                    trade_range[0] if trade_range else None,
                    trade_range[1] if trade_range else None,
                    (market.funding or {}).get(instrument),
                )
            for key, portfolio in lab.portfolios.items():
                state = json.dumps(serialize_portfolio(portfolio))
                await connection.execute(
                    """INSERT INTO lab_portfolios(experiment_id,strategy_key,state,updated_at)
                    VALUES($1,$2,$3::jsonb,$4) ON CONFLICT(experiment_id,strategy_key)
                    DO UPDATE SET state=EXCLUDED.state,updated_at=EXCLUDED.updated_at""",
                    experiment_id, key, state, market.timestamp,
                )
                await connection.execute(
                    """INSERT INTO lab_equity(experiment_id,strategy_key,timestamp,balance,equity)
                    VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""",
                    experiment_id, key, minute, portfolio.balance, portfolio.equity,
                )
                for trade in portfolio.trades:
                    await connection.execute(
                        """INSERT INTO lab_trades(id,experiment_id,strategy_key,status,opened_at,closed_at,data)
                        VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT(id) DO UPDATE SET
                        status=EXCLUDED.status,closed_at=EXCLUDED.closed_at,data=EXCLUDED.data""",
                        trade.id, experiment_id, key, trade.status, trade.opened_at,
                        trade.closed_at, json.dumps(serialize_trade(trade)),
                    )
            if events:
                await connection.executemany(
                    "INSERT INTO lab_events(experiment_id,timestamp,strategy_key,kind,message,trade_id) VALUES($1,$2,$3,$4,$5,$6)",
                    [(experiment_id, event.timestamp, event.strategy, event.kind, event.message, event.trade_id) for event in events],
                )

    async def save_candle(self, experiment_id: str, candle: Candle) -> None:
        await self._pool().execute(
            """INSERT INTO lab_candles(experiment_id,symbol,instrument_kind,minutes,start_at,open,high,low,close,volume)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT DO NOTHING""",
            experiment_id, candle.instrument.symbol, candle.instrument.kind, candle.minutes,
            candle.start, candle.open, candle.high, candle.low, candle.close, candle.volume,
        )

    async def log_event(self, experiment_id: str, event: LabEvent) -> None:
        await self._pool().execute(
            "INSERT INTO lab_events(experiment_id,timestamp,strategy_key,kind,message,trade_id) VALUES($1,$2,$3,$4,$5,$6)",
            experiment_id, event.timestamp, event.strategy, event.kind, event.message, event.trade_id,
        )

    async def complete(self, experiment_id: str, timestamp: datetime) -> None:
        await self._pool().execute(
            "UPDATE lab_experiments SET status='COMPLETED',completed_at=$2 WHERE id=$1 AND status='RUNNING'",
            experiment_id, timestamp,
        )

    async def equity_rows(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """SELECT * FROM lab_equity WHERE experiment_id=$1
            AND (extract(minute FROM timestamp)::int % 15=0 OR timestamp>=now()-interval '5 minutes')
            ORDER BY timestamp,strategy_key""",
            experiment_id,
        )
        return [dict(row) for row in rows]

    async def dashboard_rows(
        self, experiment_id: str, *, include_equity: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        portfolios = await self._pool().fetch(
            "SELECT strategy_key,state,updated_at FROM lab_portfolios WHERE experiment_id=$1 ORDER BY strategy_key",
            experiment_id,
        )
        events = await self._pool().fetch(
            "SELECT * FROM lab_events WHERE experiment_id=$1 ORDER BY timestamp DESC LIMIT 100",
            experiment_id,
        )
        drawdowns = await self._pool().fetch(
            """SELECT strategy_key,max(CASE WHEN peak=0 THEN 0 ELSE (peak-equity)/peak END) AS max_drawdown
            FROM (SELECT strategy_key,equity,max(equity) OVER (
                PARTITION BY strategy_key ORDER BY timestamp ROWS UNBOUNDED PRECEDING
            ) AS peak FROM lab_equity WHERE experiment_id=$1) samples GROUP BY strategy_key""",
            experiment_id,
        )
        return {
            "portfolios": [{
                "strategy_key": row["strategy_key"],
                "state": json.loads(row["state"]) if isinstance(row["state"], str) else row["state"],
                "updated_at": row["updated_at"],
            } for row in portfolios],
            "equity": await self.equity_rows(experiment_id) if include_equity else [],
            "events": [dict(row) for row in events],
            "drawdowns": [dict(row) for row in drawdowns],
        }
