# Crypto strategy lab

This service runs seven isolated paper portfolios against the same ordered
Hyperliquid market feed. It never signs or submits an order.

The forward test includes:

- cash and buy-and-hold BTC benchmarks;
- BTC trend breakout;
- BTC volatility breakout;
- post-only maker mean reversion;
- BTC and ETH pairs mean reversion;
- spot and perpetual funding carry.

Each portfolio starts with $10,000. The paper broker models bid and ask spread,
adverse slippage, separate spot and perpetual maker/taker fees, hourly funding,
multi-leg fills, resting post-only orders, and forced closing at the experiment
deadline. A daily 2% stop, 15% total drawdown stop, and 2x gross exposure cap
pass through one shared risk gate.

## Run it locally

Python 3.13 and PostgreSQL are required.

```bash
uv sync
cp .env.example .env
uv run trade-simulation lab-serve
```

In another terminal, create the official run:

```bash
GIT_COMMIT=$(git rev-parse HEAD) uv run trade-simulation lab-init
```

The command prints every frozen cost, strategy, and risk setting. It creates no
data until you type `START STRATEGY LAB` exactly.

Open [http://localhost:8000](http://localhost:8000). The dashboard has strategy
rankings, equity curves, execution and funding costs, open state, trades, and
feed/risk events. Read-only endpoints are available at `/api/dashboard`,
`/api/equity`, `/api/experiment`, and `/health`.

Run the checks with:

```bash
uv run python -m unittest discover -s tests -v
```

## Deploy after the current v1 run

Do not replace the running v1 container until its scheduled test ends. Export
that run first:

```bash
docker compose exec app trade-simulation report --final
docker compose cp app:/app/reports ./v1-reports
```

Then deploy this branch. The v2 tables use a `lab_` prefix, so the old database
records can stay in PostgreSQL.

```bash
git pull
cp .env.example .env
```

Set the same strong PostgreSQL password in `POSTGRES_PASSWORD` and
`DATABASE_URL`. Set `GIT_COMMIT` to `git rev-parse HEAD`. Review the `LAB_*`,
fee, slippage, and duration values before building.

```bash
docker compose build
docker compose up -d
docker compose logs -f app
```

Create the new run only after the health endpoint reports a connected market
feed:

```bash
curl http://127.0.0.1:8000/health
docker compose exec app trade-simulation lab-init
```

The existing Caddy route is enough:

```caddyfile
trades.kelche.co {
    reverse_proxy 127.0.0.1:8000
}
```

Cloudflare should use Full (strict) SSL. Keep the app port bound to localhost as
configured in `docker-compose.yml`.

## Reports

Download strategy, trade, equity, and event CSV files from the dashboard, or
write a complete report bundle to the persistent Docker volume:

```bash
docker compose exec app trade-simulation lab-report
docker compose cp app:/app/reports ./strategy-lab-reports
```

The report directory contains the dashboard payload as JSON plus separate CSV
files. One month is still a small sample. Treat the results as evidence about
execution and strategy behavior, not proof of future profit.

## Legacy commands

The old implementation remains available as `serve`, `init`, `verify-feed`, and
`report`. Docker now starts `lab-serve` by default.
