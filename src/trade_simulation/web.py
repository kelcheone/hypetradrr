from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from .config import Settings
from .reporting import csv_text, dashboard_payload, serializable
from .service import Runtime

settings = Settings.from_env()
runtime = Runtime(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(
    title="BTC Leverage Lab",
    description="Read-only dashboard for the BTC paper-trading experiment",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    return Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


@app.get("/health")
async def health() -> dict[str, object]:
    experiment = await runtime.database.running_experiment()
    feed = runtime.feed_status()
    return {
        "status": "healthy" if runtime.feed_connected and feed["age_seconds"] is not None and feed["age_seconds"] <= settings.stale_seconds else "degraded",
        "paper_only": settings.paper_only,
        "market_feed_connected": runtime.feed_connected,
        "last_market_tick": feed["last_tick"],
        "last_market_tick_age_seconds": feed["age_seconds"],
        "database_connected": runtime.database.pool is not None,
        "experiment_status": experiment["status"] if experiment else "NOT_RUNNING",
    }


@app.get("/api/dashboard")
async def dashboard_data() -> dict[str, object]:
    experiment = await runtime.database.experiment()
    if not experiment:
        return {"experiment": None, "feed": runtime.feed_status(), "accounts": [], "trades": [], "signals": [], "equity": [], "events": []}
    rows = await runtime.database.dashboard_rows(experiment["id"], include_equity=False)
    return dashboard_payload(experiment, rows, runtime.feed_status())


@app.get("/api/experiment")
async def experiment_data() -> dict[str, object]:
    experiment = await runtime.database.experiment()
    if not experiment:
        raise HTTPException(404, "No experiment has been initialized")
    return dashboard_payload(
        experiment, await runtime.database.dashboard_rows(experiment["id"], include_equity=False),
        runtime.feed_status(),
    )


@app.get("/api/equity")
async def equity_data() -> list[dict[str, object]]:
    experiment = await runtime.database.experiment()
    if not experiment:
        return []
    return serializable(await runtime.database.equity_rows(experiment["id"], sampled=True))


@app.get("/api/export/{kind}.csv", response_class=PlainTextResponse)
async def export_csv(kind: str) -> PlainTextResponse:
    experiment = await runtime.database.experiment()
    if not experiment:
        raise HTTPException(404, "No experiment has been initialized")
    try:
        rows = await runtime.database.export_rows(experiment["id"], kind)
    except KeyError as exc:
        raise HTTPException(404, "Unknown export") from exc
    return PlainTextResponse(
        csv_text(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
    )
