from __future__ import annotations

import argparse
import asyncio
import os
import sys

import uvicorn

from .config import Settings
from .database import Database
from .hyperliquid import bbo_prices, margin_metadata
from .hyperliquid import spot_coin
from .lab_config import LabSettings
from .lab_database import LabDatabase
from .lab_reporting import write_report_files as write_lab_report_files
from .lab_service import build_lab, experiment_configuration
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


async def initialize_lab(settings: LabSettings) -> None:
    lab = build_lab(settings)
    spot_market = await spot_coin()
    configuration = experiment_configuration(settings, lab, spot_market)
    print("\nPAPER-ONLY MULTI-STRATEGY LAB")
    print(f"Portfolios: {len(lab.strategies)} | Balance: ${settings.starting_balance} each")
    print(f"Duration: {settings.duration_days} days | Spot market: {spot_market}")
    print("Strategies: " + ", ".join(strategy.key for strategy in lab.strategies))
    print(
        f"Costs: perp {settings.perp_taker_fee:.4%}/{settings.perp_maker_fee:.4%} "
        f"taker/maker; spot {settings.spot_taker_fee:.4%}/{settings.spot_maker_fee:.4%}; "
        f"slippage {settings.slippage:.4%}\n"
    )
    if input('Type "START STRATEGY LAB" to create the official run: ') != "START STRATEGY LAB":
        print("Cancelled; no experiment was created.")
        return
    database = LabDatabase(settings.database_url)
    await database.connect()
    try:
        experiment = await database.create_experiment(
            lab,
            duration_days=settings.duration_days,
            git_commit=os.getenv("GIT_COMMIT", "UNCOMMITTED"),
            configuration=configuration,
        )
        print(f"Started {experiment['id']}; ends at {experiment['ends_at'].isoformat()}")
    finally:
        await database.close()


async def report_lab(settings: LabSettings) -> None:
    database = LabDatabase(settings.database_url)
    await database.connect()
    try:
        experiment = await database.experiment()
        if not experiment:
            raise RuntimeError("No strategy experiment has been initialized")
        print(await write_lab_report_files(database, experiment, settings.reports_dir))
    finally:
        await database.close()


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
    commands.add_parser("lab-init", help="initialize the multi-strategy forward test")
    commands.add_parser("lab-serve", help="run the v2 feed, paper broker, API, and dashboard")
    commands.add_parser("lab-report", help="export the v2 strategy report and CSV datasets")
    report_parser = commands.add_parser("report", help="write report files on demand")
    report_parser.add_argument("--final", action="store_true", help="also export all CSV datasets")
    args = parser.parse_args()
    try:
        if args.command == "lab-init":
            asyncio.run(initialize_lab(LabSettings.from_env()))
        elif args.command == "lab-serve":
            lab_settings = LabSettings.from_env()
            uvicorn.run("trade_simulation.lab_web:app", host=lab_settings.host, port=lab_settings.port)
        elif args.command == "lab-report":
            asyncio.run(report_lab(LabSettings.from_env()))
        elif args.command == "init":
            settings = Settings.from_env()
            asyncio.run(initialize(settings))
        elif args.command == "verify-feed":
            settings = Settings.from_env()
            asyncio.run(verify_feed(settings))
        elif args.command == "report":
            settings = Settings.from_env()
            asyncio.run(report(settings, args.final))
        else:
            settings = Settings.from_env()
            uvicorn.run("trade_simulation.web:app", host=settings.host, port=settings.port)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
