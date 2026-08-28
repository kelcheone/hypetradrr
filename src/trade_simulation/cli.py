from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from .config import Settings
from .database import Database
from .hyperliquid import bbo_prices, margin_metadata
from .reporting import write_report_files


async def initialize(settings: Settings) -> None:
    metadata = await margin_metadata(settings.symbol)
    if max(settings.leverages) > int(metadata["max_leverage"]):
        raise RuntimeError(
            f"Configured {max(settings.leverages)}x exceeds Hyperliquid {settings.symbol} maximum "
            f"of {metadata['max_leverage']}x"
        )
    print("\nPAPER-ONLY BTC EXPERIMENT")
    print(f"Accounts: {len(settings.leverages) * 2} | Balance: ${settings.starting_balance} each")
    print(f"Leverages: {', '.join(f'{value}x' for value in settings.leverages)}")
    print(f"Duration: {settings.duration_days} days | LONG + SHORT | Funding: NOT MODELED")
    print(f"Hyperliquid margin table: {metadata['margin_table_id']}\n")
    if input('Type "START EXPERIMENT" to create and start it: ') != "START EXPERIMENT":
        print("Cancelled; no experiment was created.")
        return
    database = Database(settings)
    await database.connect()
    try:
        experiment = await database.create_experiment(metadata)
        print(f"Started {experiment['id']}; ends at {experiment['ends_at'].isoformat()}")
    finally:
        await database.close()


async def verify_feed(settings: Settings) -> None:
    metadata = await margin_metadata(settings.symbol)
    print(f"{settings.symbol} margin metadata: {metadata}")
    count = 0
    async for tick in bbo_prices(settings.symbol, settings.stale_seconds):
        print(f"{tick.timestamp.isoformat()} bid={tick.bid} ask={tick.ask} mid={tick.mid}")
        count += 1
        if count == 5:
            break


async def report(settings: Settings, final: bool) -> None:
    database = Database(settings)
    await database.connect()
    try:
        experiment = await database.experiment()
        if not experiment:
            raise RuntimeError("No experiment has been initialized")
        path = await write_report_files(database, experiment, settings.reports_dir, final)
        print(path)
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="BTC leveraged paper-trading experiment")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize and explicitly start the official experiment")
    commands.add_parser("serve", help="run the feed, simulator, API, and dashboard")
    commands.add_parser("verify-feed", help="read five live Hyperliquid BBO samples")
    report_parser = commands.add_parser("report", help="write report files on demand")
    report_parser.add_argument("--final", action="store_true", help="also export all CSV datasets")
    args = parser.parse_args()
    settings = Settings.from_env()
    try:
        if args.command == "init":
            asyncio.run(initialize(settings))
        elif args.command == "verify-feed":
            asyncio.run(verify_feed(settings))
        elif args.command == "report":
            asyncio.run(report(settings, args.final))
        else:
            uvicorn.run("trade_simulation.web:app", host=settings.host, port=settings.port)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
