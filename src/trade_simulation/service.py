from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import Settings
from .database import Database
from .engine import Price, ReversalStrategy
from .hyperliquid import bbo_prices, margin_metadata
from .reporting import write_report_files


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings)
        self.strategy = ReversalStrategy(settings)
        self.experiment: dict[str, Any] | None = None
        self.current_tick: Price | None = None
        self.feed_connected = False
        self.feed_error: str | None = None
        self.started_at = datetime.now(timezone.utc)
        self.maintenance_rate = Decimal("0.0125")
        self._task: asyncio.Task[None] | None = None
        self._last_snapshot_minute: datetime | None = None
        self._report_date = datetime.now(timezone.utc).date()

    async def start(self) -> None:
        await self.database.connect()
        self.experiment = await self.database.running_experiment()
        if self.experiment:
            metadata = self.experiment["configuration"].get("hyperliquid_margin", {})
            self.maintenance_rate = Decimal(metadata.get("maintenance_rate", "0.0125"))
            await self.database.restore_strategy(self.strategy, self.experiment)
            await self.database.event(self.experiment["id"], "APPLICATION_RESTART", "Application started or resumed")
        self._task = asyncio.create_task(self._run(), name="hyperliquid-paper-runner")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.database.close()

    async def _run(self) -> None:
        delay = 1
        while True:
            try:
                async for tick in bbo_prices(self.settings.symbol, self.settings.stale_seconds):
                    reconnected = not self.feed_connected
                    self.feed_connected = True
                    self.feed_error = None
                    delay = 1
                    self.current_tick = tick
                    if reconnected and self.experiment:
                        await self.database.event(
                            self.experiment["id"], "MARKET_FEED_CONNECTED", "Hyperliquid BBO feed connected"
                        )
                    await self._on_tick(tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.feed_connected = False
                self.feed_error = str(exc)
                if self.experiment:
                    await self.database.event(
                        self.experiment["id"], "MARKET_FEED_ERROR", str(exc), "ERROR"
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _on_tick(self, tick: Price) -> None:
        if not self.experiment:
            self.experiment = await self.database.running_experiment()
            if self.experiment:
                metadata = self.experiment["configuration"]["hyperliquid_margin"]
                self.maintenance_rate = Decimal(metadata["maintenance_rate"])
                await self.database.restore_strategy(self.strategy, self.experiment)
        if not self.experiment:
            return
        if self.experiment["status"] != "RUNNING":
            return
        if tick.timestamp >= self.experiment["ends_at"]:
            await self.database.force_complete(self.experiment, tick)
            self.experiment = await self.database.experiment(self.experiment["id"])
            await write_report_files(self.database, self.experiment, self.settings.reports_dir, final=True)
            return
        if tick.timestamp.date() != self._report_date:
            await write_report_files(
                self.database, self.experiment, self.settings.reports_dir,
                final=False, report_date=self._report_date,
            )
            await self.database.roll_day(self.experiment["id"], tick.timestamp.date())
            self._report_date = tick.timestamp.date()
        snapshot_minute = tick.timestamp.replace(second=0, microsecond=0)
        snapshot = snapshot_minute != self._last_snapshot_minute
        if snapshot:
            self._last_snapshot_minute = snapshot_minute
        await self.database.mark_positions(self.experiment["id"], tick, snapshot=snapshot)
        signals = self.strategy.on_price(tick)
        for signal in signals:
            opened = await self.database.process_signal(
                self.experiment, signal, tick.mid, self.maintenance_rate
            )
            await self.database.event(
                self.experiment["id"], "SIGNAL_CREATED", signal.reason,
                metadata={"signal_id": signal.id, "direction": signal.direction.value, "accounts_opened": opened},
            )
        # State is persisted after deterministic signal IDs/positions, so a crash retries safely.
        await self.database.record_price(self.experiment["id"], tick, self.strategy.state())

    def feed_status(self) -> dict[str, Any]:
        age = None
        if self.current_tick:
            age = max(0, (datetime.now(timezone.utc) - self.current_tick.timestamp).total_seconds())
        return {
            "connected": self.feed_connected,
            "price": self.current_tick.mid if self.current_tick else None,
            "bid": self.current_tick.bid if self.current_tick else None,
            "ask": self.current_tick.ask if self.current_tick else None,
            "last_tick": self.current_tick.timestamp if self.current_tick else None,
            "age_seconds": age,
            "error": self.feed_error,
        }
