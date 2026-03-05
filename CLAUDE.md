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

**Article-to-ticker matching** (`utils/helpers.py:extract_ticker_hints`):
- Word-boundary regex matches company names, aliases, and broad sector keywords against article text
- `config/settings.py:build_search_terms()` assembles `{ticker: [name, alias1, ..., broad_terms]}` from `watchlist.yaml` + `_SECTOR_BROAD_TERMS`
- Broad terms (e.g. "China bank", "HK MTR") ensure general sector articles are captured even without specific company mentions

**Configuration** (`config/`):
- `watchlist.yaml` — all tickers organized by sector; each entry has `ticker`, `name`, `aliases`
- `rss_feeds.yaml` — 3 general feeds (SCMP, Bloomberg, CNBC Asia) + 1 Google News RSS feed per sector
- `settings.py` — loads both YAMLs, exposes `build_search_terms()`, `get_all_tickers()`, `get_sector_for_ticker()`, etc.
- `.env` — optional `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `CLAUDE_API_KEY`

**Storage** (`storage/`):
- 5 tables: `articles`, `article_tickers`, `sentiment_scores`, `ticker_signals`, `sector_signals`
- SQLite WAL mode; `data/sentiment.db` is gitignored and auto-created at runtime
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
