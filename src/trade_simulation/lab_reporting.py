from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


ZERO = Decimal(0)
NAMES = {
    "cash-benchmark": "Cash",
    "btc-benchmark": "Buy & hold BTC",
    "trend-breakout": "Trend breakout",
    "volatility-breakout": "Volatility breakout",
    "maker-reversion": "Maker mean reversion",
    "btc-eth-pairs": "BTC–ETH pairs",
    "funding-carry": "Funding carry",
}


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _drawdown(rows: list[dict[str, Any]]) -> Decimal:
    peak = ZERO
    maximum = ZERO
    for row in sorted(rows, key=lambda item: item["timestamp"]):
        equity = _decimal(row["equity"])
        peak = max(peak, equity)
        if peak:
            maximum = max(maximum, (peak - equity) / peak)
    return maximum


def dashboard_payload(
    experiment: dict[str, Any],
    rows: dict[str, list[dict[str, Any]]],
    feed: dict[str, Any],
) -> dict[str, Any]:
    equity_by_strategy: dict[str, list[dict[str, Any]]] = {}
    for row in rows["equity"]:
        equity_by_strategy.setdefault(row["strategy_key"], []).append(row)
    stored_drawdown = {
        row["strategy_key"]: _decimal(row["max_drawdown"])
        for row in rows.get("drawdowns", [])
    }

    strategies = []
    trades = []
    for row in rows["portfolios"]:
        key = row["strategy_key"]
        state = row["state"]
        closed = [trade for trade in state["trades"] if trade["status"] == "CLOSED"]
        wins = [trade for trade in closed if _decimal(trade["net_pnl"]) > ZERO]
        losses = [trade for trade in closed if _decimal(trade["net_pnl"]) < ZERO]
        gross_profit = sum((_decimal(trade["net_pnl"]) for trade in wins), ZERO)
        gross_loss = abs(sum((_decimal(trade["net_pnl"]) for trade in losses), ZERO))
        fees = sum((_decimal(trade["fees"]) for trade in state["trades"]), ZERO)
        funding = sum((_decimal(trade["funding"]) for trade in state["trades"]), ZERO)
        net_pnl = sum((_decimal(trade["net_pnl"]) for trade in closed), ZERO)
        starting = _decimal(state["starting_balance"])
        equity = _decimal(state["equity"])
        open_trade = next((trade for trade in state["trades"] if trade["status"] == "OPEN"), None)
        strategies.append({
            "key": key,
            "name": NAMES.get(key, key.replace("-", " ").title()),
            "status": state["status"],
            "balance": _decimal(state["balance"]),
            "equity": equity,
            "return": equity / starting - 1,
            "gross_pnl": sum((_decimal(trade["gross_pnl"]) for trade in closed), ZERO),
            "fees": fees,
            "funding": funding,
            "net_pnl": net_pnl,
            "closed_trades": len(closed),
            "win_rate": Decimal(len(wins)) / len(closed) if closed else ZERO,
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "expectancy": net_pnl / len(closed) if closed else ZERO,
            "max_drawdown": stored_drawdown.get(key, _drawdown(equity_by_strategy.get(key, []))),
            "open_trade": open_trade,
            "pending_order": state.get("pending_order"),
            "updated_at": row["updated_at"],
        })
        trades.extend({**trade, "strategy_key": key, "strategy_name": NAMES.get(key, key)} for trade in state["trades"])

    strategies.sort(key=lambda item: item["return"], reverse=True)
    trades.sort(key=lambda item: item["opened_at"], reverse=True)
    return _json({
        "experiment": experiment,
        "feed": feed,
        "summary": {
            "strategies": len(strategies),
            "closed_trades": sum(item["closed_trades"] for item in strategies),
            "open_trades": sum(item["open_trade"] is not None for item in strategies),
            "total_fees": sum((item["fees"] for item in strategies), ZERO),
            "total_funding": sum((item["funding"] for item in strategies), ZERO),
        },
        "strategies": strategies,
        "trades": trades[:200],
        "equity": rows["equity"],
        "events": rows["events"],
    })


def csv_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), extrasaction="ignore")
    writer.writeheader()
    writer.writerows({
        key: json.dumps(value) if isinstance(value, (dict, list)) else value
        for key, value in row.items()
    } for row in _json(rows))
    return output.getvalue()


async def write_report_files(
    database: Any, experiment: dict[str, Any], reports_dir: str,
) -> Path:
    root = Path(reports_dir) / str(experiment["id"])
    root.mkdir(parents=True, exist_ok=True)
    payload = dashboard_payload(
        experiment, await database.dashboard_rows(experiment["id"]), {"connected": False},
    )
    (root / "strategy_lab.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    for kind, rows in {
        "strategies": payload["strategies"],
        "trades": payload["trades"],
        "equity": payload["equity"],
        "events": payload["events"],
    }.items():
        (root / f"{kind}.csv").write_text(csv_text(rows), encoding="utf-8")
    return root
