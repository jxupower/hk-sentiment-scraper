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

**Storage** (`storage/`):
- 7 tables: `articles`, `article_tickers`, `sentiment_scores`, `ticker_signals`, `sector_signals`, `securities` (full HKEX universe + watchlist flags), `fundamentals_snapshots` (daily yfinance .info ratios, append-only)
- SQLite WAL mode; `data/sentiment.db` is gitignored and auto-created at runtime
- HKEX universe ingested from `data/cache/hkex_YYYYMMDD.xlsx` (auto-downloaded by `universe/hkex_loader.py`); reconciled into `securities` by `universe/reconciler.py`. Reconciler deactivates rows that disappear from HKEX (skipped on offline-fallback path).
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
