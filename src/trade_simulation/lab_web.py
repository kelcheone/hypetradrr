from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from .lab_config import LabSettings
from .lab_reporting import csv_text, dashboard_payload
from .lab_service import LabRuntime


settings = LabSettings.from_env()
runtime = LabRuntime(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(
    title="Crypto Strategy Lab",
    description="Read-only multi-strategy paper-trading dashboard",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    return Path(__file__).with_name("lab_dashboard.html").read_text(encoding="utf-8")


@app.get("/health")
async def health() -> dict[str, object]:
    experiment = await runtime.database.running_experiment()
    feed = runtime.feed_status()
    fresh = feed["connected"] and feed["age_seconds"] is not None and feed["age_seconds"] <= settings.stale_seconds
    return {
        "status": "healthy" if fresh else "degraded",
        "paper_only": settings.paper_only,
        "market_feed_connected": feed["connected"],
        "last_market_update": feed["last_update"],
        "last_market_update_age_seconds": feed["age_seconds"],
        "database_connected": runtime.database.pool is not None,
        "experiment_status": experiment["status"] if experiment else "NOT_RUNNING",
    }


@app.get("/api/dashboard")
async def dashboard_data() -> dict[str, object]:
    experiment = await runtime.database.experiment()
    if not experiment:
        return {
            "experiment": None, "feed": runtime.feed_status(), "summary": {},
            "strategies": [], "trades": [], "events": [],
        }
    rows = await runtime.database.dashboard_rows(experiment["id"], include_equity=False)
    return dashboard_payload(experiment, rows, runtime.feed_status())


@app.get("/api/equity")
async def equity_data() -> list[dict[str, object]]:
    experiment = await runtime.database.experiment()
    if not experiment:
        return []
    return dashboard_payload(
        experiment,
        {"portfolios": [], "equity": await runtime.database.equity_rows(experiment["id"]), "events": [], "drawdowns": []},
        {},
    )["equity"]


@app.get("/api/experiment")
async def experiment_data() -> dict[str, object]:
    experiment = await runtime.database.experiment()
    if not experiment:
        raise HTTPException(404, "No strategy experiment has been initialized")
    return experiment


@app.get("/api/export/{kind}.csv", response_class=PlainTextResponse)
async def export_csv(kind: str) -> PlainTextResponse:
    experiment = await runtime.database.experiment()
    if not experiment:
        raise HTTPException(404, "No strategy experiment has been initialized")
    payload = dashboard_payload(
        experiment, await runtime.database.dashboard_rows(experiment["id"]), runtime.feed_status(),
    )
    if kind not in {"strategies", "trades", "equity", "events"}:
        raise HTTPException(404, "Unknown export")
    return PlainTextResponse(
        csv_text(payload[kind]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
    )
