from __future__ import annotations

import csv
import io
import json
import random
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


def _number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def serializable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serializable(item) for item in value]
    return _number(value)


def trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.get("net_pnl") is not None]
    wins = [trade for trade in closed if trade["net_pnl"] > 0]
    losses = [trade for trade in closed if trade["net_pnl"] < 0]
    net = sum((trade["net_pnl"] for trade in closed), Decimal(0))
    gross_profit = sum((max(trade["gross_pnl"], Decimal(0)) for trade in closed), Decimal(0))
    gross_loss = abs(sum((min(trade["gross_pnl"], Decimal(0)) for trade in closed), Decimal(0)))
    fees = sum((trade["entry_fee"] + trade["exit_fee"] for trade in closed), Decimal(0))
    slippage = sum((trade["slippage_cost"] for trade in closed), Decimal(0))
    durations = [
        (trade["exit_timestamp"] - trade["entry_timestamp"]).total_seconds()
        for trade in closed if trade.get("exit_timestamp")
    ]

    def average(items: list[dict[str, Any]], key: str) -> Decimal:
        return sum((item[key] for item in items), Decimal(0)) / len(items) if items else Decimal(0)

    streak = longest = 0
    for trade in sorted(closed, key=lambda item: item["exit_timestamp"]):
        streak = streak + 1 if trade["net_pnl"] < 0 else 0
        longest = max(longest, streak)
    return {
        "trades": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": Decimal(len(wins)) / len(closed) if closed else Decimal(0),
        "gross_pnl": sum((trade["gross_pnl"] for trade in closed), Decimal(0)),
        "net_pnl": net, "fees": fees, "slippage": slippage,
        "average_win": average(wins, "net_pnl"),
        "average_loss": average(losses, "net_pnl"),
        "largest_win": max((trade["net_pnl"] for trade in wins), default=Decimal(0)),
        "largest_loss": min((trade["net_pnl"] for trade in losses), default=Decimal(0)),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "break_even_win_rate": abs(average(losses, "net_pnl")) / (
            average(wins, "net_pnl") + abs(average(losses, "net_pnl"))
        ) if wins and losses else None,
        "expectancy": net / len(closed) if closed else Decimal(0),
        "average_holding_seconds": sum(durations) / len(durations) if durations else 0,
        "liquidations": sum(trade.get("exit_reason") == "LIQUIDATED" for trade in closed),
        "longest_losing_streak": longest,
    }


def dashboard_payload(
    experiment: dict[str, Any], rows: dict[str, list[dict[str, Any]]],
    feed: dict[str, Any],
) -> dict[str, Any]:
    positions = rows["positions"]
    account_metrics = []
    for account in rows["accounts"]:
        trades = [trade for trade in positions if trade["account_id"] == account["id"]]
        metrics = trade_metrics(trades)
        metrics.update({
            "id": account["id"], "name": account["name"], "group": account["sizing_group"],
            "leverage": account["leverage"], "balance": account["balance"],
            "equity": account["equity"], "return": (account["equity"] / account["starting_balance"]) - 1,
            "max_drawdown": account["max_drawdown"], "status": account["status"],
            "return_over_drawdown": ((account["equity"] / account["starting_balance"]) - 1) / account["max_drawdown"] if account["max_drawdown"] else None,
            "long": trade_metrics([trade for trade in trades if trade["direction"] == "LONG"]),
            "short": trade_metrics([trade for trade in trades if trade["direction"] == "SHORT"]),
        })
        account_metrics.append(metrics)
    directional = {
        direction: trade_metrics([trade for trade in positions if trade["direction"] == direction])
        for direction in ("LONG", "SHORT")
    }
    directional["COMBINED"] = trade_metrics(positions)
    open_positions = [trade.copy() for trade in positions if trade["status"] == "OPEN"]
    current_price = feed.get("price")
    for position in open_positions:
        if current_price is not None:
            sign = Decimal(1) if position["direction"] == "LONG" else Decimal(-1)
            position["unrealized_pnl"] = position["quantity"] * (current_price - position["entry_price"]) * sign
            position["distance_to_liquidation"] = abs(current_price - position["liquidation_price"]) / current_price
        position["holding_seconds"] = (datetime.now(position["entry_timestamp"].tzinfo) - position["entry_timestamp"]).total_seconds()
    return serializable({
        "experiment": experiment,
        "feed": feed,
        "summary": {
            "signals": len(rows["signals"]),
            "long_signals": sum(signal["direction"] == "LONG" for signal in rows["signals"]),
            "short_signals": sum(signal["direction"] == "SHORT" for signal in rows["signals"]),
            "open_positions": len(open_positions),
            "closed_trades": sum(trade["status"] != "OPEN" for trade in positions),
            "fees": sum((trade["entry_fee"] + trade["exit_fee"] for trade in positions), Decimal(0)),
        },
        "accounts": account_metrics,
        "directional": directional,
        "open_positions": open_positions,
        "trades": positions[:200],
        "signals": rows["signals"],
        "equity": rows["equity"],
        "events": rows["events"],
    })


def csv_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), extrasaction="ignore")
    writer.writeheader()
    writer.writerows([{key: _number(value) for key, value in row.items()} for row in rows])
    return output.getvalue()


def monte_carlo(
    trades: list[dict[str, Any]], starting_balance: Decimal,
    observed_days: int, paths: int = 1000,
) -> dict[str, Any] | None:
    outcomes = [trade["net_pnl"] for trade in trades if trade.get("net_pnl") is not None]
    if not outcomes:
        return None
    count = max(1, round(len(outcomes) / observed_days * 30))
    rng = random.Random(42)
    balances = sorted(
        starting_balance + sum((rng.choice(outcomes) for _ in range(count)), Decimal(0))
        for _ in range(paths)
    )
    percentile = lambda value: balances[round((paths - 1) * value)]
    return {
        "trades_per_path": count,
        "p5": percentile(0.05), "p25": percentile(0.25), "median": percentile(0.5),
        "p75": percentile(0.75), "p95": percentile(0.95),
        "probability_loss_over_20pct": sum(value < starting_balance * Decimal("0.8") for value in balances) / paths,
        "probability_loss_over_50pct": sum(value < starting_balance * Decimal("0.5") for value in balances) / paths,
        "estimated_risk_of_ruin": sum(value <= 0 for value in balances) / paths,
    }


async def write_report_files(
    database: Any, experiment: dict[str, Any], reports_dir: str, final: bool,
    report_date: date | None = None,
) -> Path:
    root = Path(reports_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows = await database.dashboard_rows(experiment["id"])
    market_context = None
    if not final:
        report_date = report_date or datetime.now().date()
        rows["positions"] = [
            row for row in rows["positions"]
            if (row.get("exit_timestamp") or row["entry_timestamp"]).date() == report_date
        ]
        rows["signals"] = [row for row in rows["signals"] if row["timestamp"].date() == report_date]
        rows["equity"] = [row for row in rows["equity"] if row["timestamp"].date() == report_date]
        rows["events"] = [row for row in rows["events"] if row["timestamp"].date() == report_date]
        prices = [
            row for row in await database.export_rows(experiment["id"], "prices")
            if row["timestamp"].date() == report_date
        ]
        if prices:
            move = prices[-1]["mid"] / prices[0]["mid"] - Decimal(1)
            regime = "UP" if move >= Decimal("0.005") else "DOWN" if move <= Decimal("-0.005") else "FLAT"
            market_context = {
                "btc_open": prices[0]["mid"], "btc_close": prices[-1]["mid"],
                "btc_move": move, "regime": regime,
            }
    payload = dashboard_payload(experiment, rows, {"connected": False, "price": experiment.get("last_price")})
    payload["market_context"] = serializable(market_context)
    stem = "final_report" if final else report_date.isoformat()
    (root / f"{stem}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    accounts = payload["accounts"]
    lines = [
        "# BTC Leveraged Paper-Trading Experiment",
        "",
        f"Status: **{experiment['status']}**  ",
        f"Started: {experiment['started_at']}  ",
        f"Ends: {experiment['ends_at']}  ",
        "Funding: **NOT MODELED**",
        "",
        "| Account | Return | Net PnL | Win rate | Max drawdown | Fees | Liquidations | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    if market_context:
        lines[7:7] = [
            f"BTC: **${market_context['btc_open']:,.2f} → ${market_context['btc_close']:,.2f}** "
            f"({market_context['btc_move']:.2%}), regime **{market_context['regime']}**. ", "",
        ]
    for account in accounts:
        lines.append(
            f"| {account['name']} | {account['return']:.2%} | ${account['net_pnl']:,.2f} | "
            f"{account['win_rate']:.2%} | {account['max_drawdown']:.2%} | "
            f"${account['fees']:,.2f} | {account['liquidations']} | {account['status']} |"
        )
    direction = payload["directional"]
    lines.extend([
        "", "## Direction contribution", "",
        "| Direction | Trades | Win rate | Gross PnL | Fees | Net PnL | Profit factor | Expectancy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for name in ("LONG", "SHORT", "COMBINED"):
        metric = direction[name]
        factor = "—" if metric["profit_factor"] is None else f"{metric['profit_factor']:.2f}"
        lines.append(
            f"| {name} | {metric['trades']} | {metric['win_rate']:.2%} | ${metric['gross_pnl']:,.2f} | "
            f"${metric['fees']:,.2f} | ${metric['net_pnl']:,.2f} | {factor} | ${metric['expectancy']:,.2f} |"
        )
    if final and accounts:
        best_raw = max(accounts, key=lambda item: item["return"])
        adjusted = [item for item in accounts if item["return_over_drawdown"] is not None]
        best_adjusted = max(adjusted, key=lambda item: item["return_over_drawdown"]) if adjusted else best_raw
        worst = min(accounts, key=lambda item: item["return"])
        lines.extend([
            "", "## Conclusion", "",
            f"- Best raw return: **{best_raw['name']}** at {best_raw['return']:.2%}.",
            f"- Best return/drawdown: **{best_adjusted['name']}**.",
            f"- Worst result: **{worst['name']}** at {worst['return']:.2%}.",
            f"- LONG expectancy: **${direction['LONG']['expectancy']:,.2f}** per simulated trade.",
            f"- SHORT expectancy: **${direction['SHORT']['expectancy']:,.2f}** per simulated trade.",
            f"- Combined expectancy: **${direction['COMBINED']['expectancy']:,.2f}** per simulated trade.",
            "", "One week is not evidence of durable profitability. Monte Carlo output is descriptive resampling, not a forecast.",
        ])
    (root / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if final:
        for kind in ("trades", "signals", "accounts", "equity", "prices"):
            (root / f"{kind}.csv").write_text(
                csv_text(await database.export_rows(experiment["id"], kind)), encoding="utf-8"
            )
        (root / "configuration.json").write_text(
            json.dumps(serializable(experiment["configuration"]), indent=2), encoding="utf-8"
        )
        daily_rows = []
        closed = [trade for trade in rows["positions"] if trade.get("exit_timestamp")]
        for day in sorted({trade["exit_timestamp"].date() for trade in closed}):
            for direction_name in ("LONG", "SHORT", "COMBINED"):
                trades = [
                    trade for trade in closed
                    if trade["exit_timestamp"].date() == day
                    and (direction_name == "COMBINED" or trade["direction"] == direction_name)
                ]
                daily_rows.append({"date": day, "direction": direction_name} | trade_metrics(trades))
        (root / "daily_metrics.csv").write_text(csv_text(daily_rows), encoding="utf-8")
        monte_carlo_rows = []
        for account in rows["accounts"]:
            trades = [trade for trade in rows["positions"] if trade["account_id"] == account["id"]]
            result = monte_carlo(
                trades, account["starting_balance"],
                int(experiment["configuration"].get("duration_days", 7)),
            )
            if result:
                monte_carlo_rows.append({"account": account["name"]} | result)
        (root / "monte_carlo.csv").write_text(csv_text(monte_carlo_rows), encoding="utf-8")
    return root / f"{stem}.md"
