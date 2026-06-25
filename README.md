# Croissant Stock Analyser

Hong Kong + US stock dashboard combining sentiment (RSS / Yahoo / Reddit) +
fundamentals (akshare / yfinance) + portfolio + risk + backtest. See
[CLAUDE.md](CLAUDE.md) for the architecture deep-dive.

## Run locally

```bash
# First-time setup
python -m venv venv
venv\Scripts\activate                          # Windows
pip install -r requirements.txt
cp .env.example .env                           # then edit with your secrets

# Launch dashboard + background scrapers at http://localhost:8050
python main.py dashboard
```

## Run via Docker (mirrors production)

```bash
docker compose up --build
```

Same image, same behaviour as the deployed environment. The local
`data/` directory is bind-mounted so `sentiment.db` persists across
`docker compose down` / `up`.

## Deploy

Free always-on deployment on Oracle Cloud + Cloudflare — see
[docs/deploy.md](docs/deploy.md) for the full runbook.

| | |
|---|---|
| Hosting | Oracle Cloud Always Free (4-core ARM, 24GB RAM, $0/mo) |
| Auth | Cloudflare Access (free up to 50 users) |
| CI/CD | GitHub Actions → ghcr.io → SSH `docker compose pull && up -d` |
| Monitoring | UptimeRobot free tier |

## CI

Every PR + push to main runs three checks via [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

| Job | What it checks |
|---|---|
| `lint` | `ruff check .` (rules E + F + W + I — see [pyproject.toml](pyproject.toml)) |
| `smoke` | Imports + dashboard boots + `/_dash-layout` returns 200 |
| `docker-build` | Multi-arch (`linux/amd64` + `linux/arm64`) image builds clean — no push |

## Project structure

See [CLAUDE.md](CLAUDE.md) for the detailed architecture. High-level:

```
analysis/     factor scoring · backtest · DCF · sentiment aggregation
config/       watchlist YAMLs · sub-sector taxonomy · settings
dashboard/    Dash app · 8 tabs · i18n · charts
scheduler/    APScheduler-driven scrape cycles
scrapers/     RSS · Yahoo Finance · Reddit · akshare price/fundamentals
storage/      SQLite + Supabase Postgres hybrid (factory pattern)
universe/     HKEX + US universe ingest + reconciler
docs/         deploy runbook
.github/      CI + CD workflows
Dockerfile, docker-compose.yml, pyproject.toml
```
