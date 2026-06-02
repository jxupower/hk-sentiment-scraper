# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate virtual environment (always required first)
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run a single scrape cycle (populates DB, prints sector signals table)
python main.py scrape --once

# Start background scraper (runs every 30 min, no dashboard)
python main.py scrape

# Launch dashboard with background scraping at http://localhost:8050
python main.py dashboard

# Print API setup instructions
python main.py setup

# Run tests
pytest

# Run a single test file
pytest scrapers/test_rss_scraper.py
```

## Architecture

This is a Hong Kong / China stock sentiment tool. It scrapes RSS feeds and Yahoo Finance (optionally Reddit), runs VADER or Claude sentiment analysis, stores results in SQLite, and displays signals in a Dash dashboard.

**Data flow each scrape cycle** (`scheduler/job_runner.py`):
1. All scrapers call `scraper.fetch(search_terms)` → `list[RawArticle]`
2. New articles inserted via `ArticleRepository`, duplicates skipped by URL
3. Each article scored by `SentimentAnalyzer` (VADER always; Claude if API key present with credits)
4. Per-ticker signals computed → `ticker_signals` table (BUY/SELL/HOLD/WATCH)
5. Per-sector signals computed → `sector_signals` table (UP/DOWN/NEUTRAL/MIXED)
6. Dashboard reads DB on `dcc.Interval` refresh

**Article-to-ticker matching** (`utils/ticker_matcher.py:TickerMatcher`):
- Single compiled alternation regex (~30ms build, ~0.2ms per article) over all known terms — built once per scrape cycle from the `securities` table by `config/settings.py:build_search_terms_from_db()`
- Two tiers per ticker:
  - **Watchlist tickers**: terms = YAML name + aliases + sector broad terms (`_SECTOR_BROAD_TERMS` in `config/settings.py`)
  - **Universe tickers**: terms = `clean_hkex_name(name)` only (strips share-class suffixes like `-W`, `-SW`, `-R`, `-S`, `-H`, `-A` so HKEX's "BABA-W" matches articles saying "Alibaba" via the watchlist tier first)
- Min term length 4 (filters out noisy 2-3 char names like "JD", "AIA", "MTR" — but their longer aliases like "MTR Corporation" still match)
- Cap at max 5 ticker tags per article; watchlist tickers always win the cap over universe tickers
- Yahoo and Reddit scrapers are restricted to watchlist terms only (per-ticker scraping doesn't scale to ~2,700 universe tickers); RSS scrapes broadly and tags both tiers via the matcher

**Configuration** (`config/`):
- `watchlist.yaml` — curated "deep" tracking list organized by sector; each entry has `ticker`, `name`, `aliases`. Reconciled into the `securities` table at every startup.
- `rss_feeds.yaml` — 3 broad business feeds (SCMP, Bloomberg, CNBC Asia) + ~8 broad Google News queries about HK/China markets. NOT per-sector — the matcher decides which tickers each article is about.
- `settings.py` — loads both YAMLs, exposes `build_search_terms_from_db()`, `clean_hkex_name()`, `get_all_tickers()`, `get_sector_for_ticker()`. `_SECTOR_BROAD_TERMS` adds catch-all keywords for each watchlist sector.
- `.env` — optional `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `CLAUDE_API_KEY`

**Storage — dual backend** (`storage/`):
The app uses a hybrid storage model: most tables stay in local SQLite, but `historical_prices` and `fundamentals_snapshots` can live in Supabase Postgres when `USE_CLOUD_DB=true` in `.env`. Routing happens in `storage/factory.py` — analysis modules import via the factory (or via `analysis/data_loader.py`) so the same code works against either backend.

- **Local SQLite** (`data/sentiment.db`, WAL mode, gitignored, auto-created): `articles`, `article_tickers`, `sentiment_scores`, `ticker_signals`, `sector_signals`, `securities`, `research_notes`, `backtest_runs`, `backtest_holdings`, `optimized_parameters`. Plus `historical_prices` + `fundamentals_snapshots` when `USE_CLOUD_DB=false`.
- **Supabase Postgres** (when `USE_CLOUD_DB=true`): only `historical_prices` and `fundamentals_snapshots`. Schema in [scripts/supabase_schema.sql](scripts/supabase_schema.sql); connection helper in [storage/cloud_db.py](storage/cloud_db.py) (psycopg2 `ThreadedConnectionPool`, max 10 conns to stay below free-tier cap); cloud-backed repos in [storage/cloud_repository.py](storage/cloud_repository.py) mirror the SQLite repo APIs so callers swap transparently.
- **Cache-aside layer**: [analysis/data_loader.py](analysis/data_loader.py) wraps both backends with on-demand fetch — `get_or_fetch_prices(ticker)` checks cache, falls back to yfinance on miss, upserts, returns; same shape for `get_or_fetch_fundamentals_history` (akshare) and `get_or_fetch_latest_fundamentals` (yfinance `.info`). The Research tab uses this so any HK ticker self-heals on first request.
- HKEX universe ingested from `data/cache/hkex_YYYYMMDD.xlsx` (auto-downloaded by `universe/hkex_loader.py`); reconciled into `securities` by `universe/reconciler.py`. Reconciler deactivates rows that disappear from HKEX (skipped on offline-fallback path). **REITs are not loaded** — known gap, requires parsing a separate sheet in the HKEX xlsx.
- Migration helper: [scripts/migrate_to_supabase.py](scripts/migrate_to_supabase.py) one-shot copies existing SQLite price+fundamentals rows into Supabase via `psycopg2.extras.execute_values` (~40s for 100K rows). [scripts/resume_historical_seed.py](scripts/resume_historical_seed.py) finishes any interrupted bulk yfinance seed for tickers that have no prices yet, with a `data/.seed_checkpoint.json` so re-runs are cheap.
- Required env vars when `USE_CLOUD_DB=true`: `SUPABASE_DB_URL` (use the **Session pooler** URI from Supabase Project Settings → Database, NOT the Direct connection URI — Direct is IPv6-only and breaks IPv4 networks; password URL-encode `@` as `%40`, `+` as `%2B`).

**Fundamentals + multi-factor scoring** (Direction C):
- Per-ticker yfinance `.info` ratios were previously stored daily by a 03:15 UTC cron. **The daily cron is now disabled** in the Supabase migration — it would write ~2,769 rows/day and exhaust the 500MB free tier within months. The Research tab fetches current ratios on demand via `analysis/data_loader.py:get_or_fetch_latest_fundamentals`. Annual akshare history (~9 years per ticker) is the primary multi-year source.
- `analysis/factor_scores.py` — `FactorScoringEngine` computes sector-relative percentile ranks (0-100) per ticker on four factors: **Value** (1/PE, 1/PB, 1/EV-EBITDA), **Quality** (ROE, ROA, -D/E), **Growth** (earnings + revenue growth), **Sentiment** (avg article sentiment over window). Composite = weighted average of available factors. Viability guards disqualify negative book value, microcaps (<HK$200M), P/E < 0.5 or > 500, profit margins < -50%. Sector-risk flags from `config/sector_risk.yaml` add informational warning badges (not disqualifiers).
- `analysis/screens.py` — rule-based screens with **absolute thresholds**: Value (P/E 5-20, P/B 0.5-3, ROE>10%), Quality Compounder (ROE≥15%, D/E<100%, +ve growth, ≥HK$10B), Income (yield≥4%, ≥HK$5B), Avoid Distress (educational — extreme cheap + ≥2 distress red flags). No scoring, pass/fail only.

**Dashboard** (`dashboard/`) — 6 tabs:
- **Sentiment** (`callbacks.py` + `layout.py:_sentiment_tab`) — original sector-card view for the 54 curated watchlist tickers
- **Screener** (`screener_layout.py` + `screener_callbacks.py`) — raw fundamentals table for all ~2,768 active universe tickers, sortable/filterable
- **Discovery** (`recommendations_layout.py` + `recommendations_callbacks.py`) — multi-factor percentile-rank candidates; 4 weight inputs (Value/Quality/Growth/Sentiment) + viability filters + sector-risk flag badges
- **Screens** (`screens_layout.py` + `screens_callbacks.py`) — 4 rule-based pass/fail screens with absolute thresholds, accessed via sub-tabs
- **Backtest** (`backtest_layout.py` + `backtest_callbacks.py`) — per-industry walk-forward optimization results for each screen; sub-tabs per screen; live "what-if" backtest button using default params
- **Stock Research** (`stock_research_layout.py` + `stock_research_callbacks.py`) — single-stock deep-dive following The Plain Bagel's 6-step framework: type a ticker → see screening context, business overview with auto-SWOT, financial CAGR + peer scorecard + forensic flags, strategy + dilution chart, valuation with 2-stage DCF sliders + sensitivity heatmap, notes/research-status workflow, devil's-advocate Claude AI, markdown export

**Stock Research framework + supporting modules** (Plain Bagel 6-step):
- `analysis/cagr.py` — multi-horizon (5/10/15y) CAGR helpers + YoY growth series
- `analysis/forensic.py` — heuristic red-flag detector: share dilution, debt explosion, margin compression, revenue/earnings divergence, sustained earnings decline
- `analysis/dcf.py` — 2-stage Gordon Growth DCF with sensitivity table. Uses `EPS × shares × 0.8` FCF proxy because akshare/yfinance don't reliably expose historical free cash flow for HK
- `analysis/peer_comparison.py` — per-ticker scorecard vs sector peers across 10 metrics with percentile ranks
- `analysis/research_orchestrator.py` — composes everything into one `ResearchReport` dataclass. Pass `skip_financial_statements=True` to skip the (3-8s on cold cache) Section 3b raw-filings fetch; the dashboard does this and loads them lazily when the user clicks "Load Financial Statements".
- `analysis/_research_cache.py` — per-process, thread-safe TTL cache (15 min) for `FactorScoringEngine.compute()` + the 4 `BUILTIN_SCREENS` results. Stops `build_research_report` from re-scoring all ~2,769 tickers and re-scanning the universe 4 times on every ticker load. First load builds the cache (~5-7s); subsequent loads within 15 min hit warm cache and skip straight to ticker-specific work (~1-3s). Restart the dashboard to force-rebuild.
- `storage/repository.py:ResearchNotesRepository` — persists SWOT, qualitative notes, DCF inputs, research-status workflow (raw/researched/watchlist/owned/rejected)
- 1 new table: `research_notes`

**Backtest + per-industry optimization** (added after Direction C):
- `scrapers/akshare_historical_scraper.py` pulls ~9 years of annual HK fundamentals via akshare (`stock_financial_hk_analysis_indicator_em`). Writes into the same `fundamentals_snapshots` table; per-share fields (`eps_ttm`, `bps`, `shares_outstanding`) populated so the backtest can derive as-of P/E and P/B by combining with `historical_prices`.
- `scrapers/historical_price_scraper.py` uses `yfinance.download(tickers=[batch], period='10y')` for bulk multi-year OHLCV. Writes to new `historical_prices` table.
- `analysis/screens.py` — each screen is now parameterized via `ScreenParams` dataclass; `BUILTIN_SCREENS[i].default_params` preserves the original hardcoded thresholds for backward compat.
- `analysis/backtest.py` — `BacktestEngine` rebalances at chosen frequency, applies screen with given params, equal-weights survivors, compares to sector-equal-weighted benchmark, computes total_return / Information Ratio / Sharpe / max drawdown / hit rate. 60-day reporting lag applied to mitigate look-ahead bias from akshare's as-restated data.
- `analysis/optimization.py` — `WalkForwardOptimizer` sweeps coarse parameter grids (`PARAM_GRIDS`), runs backtests across rolling train+test windows, picks per-(screen, industry) params that maximize avg Information Ratio. Persists to `optimized_parameters`.
- 4 new tables: `historical_prices`, `backtest_runs`, `backtest_holdings`, `optimized_parameters`.
- New CLI commands:
  ```bash
  python main.py historical seed --tickers ALL --price-period 10y     # ~6-8h one-time
  python main.py backtest run --screen value --start 2020-01-01       # ad-hoc
  python main.py backtest optimize --screen value                     # walk-forward
  ```
- New scheduled jobs in `JobRunner`: weekly `_refresh_historical_prices` (Sun 04:00 UTC), monthly `_reoptimize_parameters` (1st 05:00 UTC).
- **Honest limitations** documented in plan + dashboard banner: (1) akshare data is as-restated not point-in-time → look-ahead bias; (2) survivor bias from missing-delisted-ticker data; (3) MVP is equal-weighted, no transaction costs; (4) HK trading calendar / holidays ignored; (5) `Income` screen has 0 historical matches because akshare doesn't expose historical dividend yield — the screen is therefore valuable only for current-day filtering, not backtesting.
- When you rename or add sectors in `watchlist.yaml`, clear stale rows from `sector_signals` and `ticker_signals`:
  ```python
  import sqlite3; conn = sqlite3.connect("data/sentiment.db")
  conn.execute("DELETE FROM sector_signals"); conn.execute("DELETE FROM ticker_signals"); conn.commit()
  ```

**Signal thresholds** (`analysis/signals.py`):
- Sector: `sentiment > 0.15 AND momentum > 0` → UP; `< -0.15 AND momentum < 0` → DOWN; conflicting → MIXED
- Ticker: `sentiment > 0.2 AND momentum > 0` → BUY; `< -0.2 AND momentum < 0` → SELL; conflicting → WATCH
- Confidence = blend of `article_count / 20` and `|sentiment| / 0.3`

**Dashboard** (`dashboard/`):
- `app.py` — Dash factory; uses DARKLY bootstrap theme; sectors list comes from `watchlist.yaml` at startup
- `layout.py` — static layout structure
- `callbacks.py` — all interactivity; reads DB directly using `sqlite3` (not through repository layer)
- `charts.py` — Plotly chart factory functions

**Scrapers** (`scrapers/`):
- All scrapers implement `BaseScraper`: `is_available()` and `fetch(search_terms) -> list[RawArticle]`
- `RssScraper` — feedparser; `is_available()` always True
- `YahooScraper` — yfinance news + `fetch_price_history(ticker, period)`; `is_available()` always True
- `RedditScraper` — PRAW; `is_available()` returns False when no credentials → silently skipped

**Sentiment** (`analysis/sentiment.py`):
- VADER compound score `[-1, 1]`; Claude model is `claude-haiku-4-5-20251001`
- Claude score replaces VADER as `final_score` when available; VADER used as fallback
- Claude failures are caught silently; requires account credits to function

## Adding or Changing Sectors

1. Edit `config/watchlist.yaml` — add/rename sector and its ticker entries
2. Edit `_SECTOR_BROAD_TERMS` in `config/settings.py` — add matching broad keywords for the sector
3. Edit `config/rss_feeds.yaml` — add a Google News RSS feed targeting the sector's companies
4. Clear stale DB signals (see Storage note above)
5. Run `python main.py scrape --once` to verify new sectors appear in the output table
