# BTC Leverage Lab

> **THIS APPLICATION IS A PAPER-TRADING EXPERIMENT. IT DOES NOT PLACE REAL
> ORDERS. PAST OR SIMULATED PERFORMANCE DOES NOT INDICATE FUTURE RESULTS.
> LEVERAGED PERPETUAL FUTURES CAN RESULT IN RAPID AND COMPLETE LOSS OF CAPITAL.**

A seven-day BTC perpetual simulation using Hyperliquid's public BBO WebSocket.
The same deterministic LONG and SHORT reversal signals are distributed to 16
independent virtual accounts:

- Risk-normalized accounts at 5×, 10×, 15×, 20×, 25×, 30×, 35×, and 40×.
- Fixed-$2,000-margin accounts at the same leverage levels.

There is no wallet configuration, private key handling, API signing, or order
submission code in this repository.

## What is modeled

- One-second BTC bid/ask/mid samples retained for replay.
- Symmetric five-minute dip/recovery and rally/reversal signals.
- Taker fees, adverse slippage, isolated maintenance margin, TP, SL, liquidation,
  cooldown, daily trade/loss limits, and total drawdown limits.
- PostgreSQL persistence, duplicate protection, restart recovery, daily reports,
  final exports, a read-only API, and a live dashboard.
- Funding is deliberately disabled and every report is labelled
  `FUNDING_NOT_MODELED`.

Risk-normalized sizing includes the planned stop, two taker fees, and two
slippage assumptions. This keeps the 5× account feasible and targets total
loss-at-stop rather than pretending execution is free.

## VM deployment

Requirements: Docker Engine with the Compose plugin and outbound HTTPS/WSS
access to `api.hyperliquid.xyz`.

```bash
git clone <your-repository-url> trade-simulation
cd trade-simulation
cp .env.example .env
```

Edit `.env` and change both occurrences of the PostgreSQL password to the same
random value. Set `GIT_COMMIT` to the output of `git rev-parse HEAD`, then review
every frozen experiment setting before continuing.

```bash
docker compose build
docker compose up -d
docker compose logs -f app
```

The app binds to `127.0.0.1:8000`; expose it through a TLS reverse proxy such
as Caddy rather than opening port 8000 publicly. Before starting the official
run, verify the feed:

```bash
docker compose exec app trade-simulation verify-feed
```

Initialize once. The command prints the immutable configuration and requires
the exact confirmation `START EXPERIMENT`:

```bash
docker compose exec app trade-simulation init
```

The already-running service detects the new experiment automatically. Check:

```bash
curl http://localhost:8000/health
docker compose logs -f app
```

Do not edit configuration or code during the official run. A critical code
change invalidates the experiment and should begin a new seven-day run.

## Reports and exports

The dashboard exposes CSV downloads for trades, signals, accounts, equity, and
sampled prices. Daily JSON/Markdown reports and the final dataset are also
written to the persistent `report-data` Docker volume.

Generate a report on demand:

```bash
docker compose exec app trade-simulation report
docker compose exec app trade-simulation report --final
```

Copy the report directory from the container if desired:

```bash
docker compose cp app:/app/reports ./reports
```

## Local development

```bash
uv sync
cp .env.example .env
# Change DATABASE_URL to postgresql://paper:paper@localhost:5432/paper
uv run trade-simulation serve
python -m unittest discover -s tests -v
```

Read-only endpoints include `/health`, `/api/dashboard`, `/api/experiment`, and
`/api/export/{trades,signals,accounts,equity,prices}.csv`. Interactive API docs
are available at `/docs`.
