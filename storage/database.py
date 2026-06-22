import sqlite3
from pathlib import Path
from utils.logger import get_logger

logger = get_logger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def initialize(self):
        with self.get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS articles (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    source       TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    body         TEXT,
                    url          TEXT UNIQUE NOT NULL,
                    published_at DATETIME,
                    author       TEXT,
                    raw_score    REAL,
                    market       TEXT NOT NULL DEFAULT 'HK',
                    fetched_at   DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS article_tickers (
                    article_id INTEGER REFERENCES articles(id) ON DELETE CASCADE,
                    ticker     TEXT NOT NULL,
                    PRIMARY KEY (article_id, ticker)
                );

                CREATE TABLE IF NOT EXISTS sentiment_scores (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id   INTEGER REFERENCES articles(id) ON DELETE CASCADE,
                    ticker       TEXT NOT NULL,
                    vader_score  REAL,
                    claude_score REAL,
                    final_score  REAL,
                    label        TEXT,
                    scored_at    DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS ticker_signals (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker              TEXT NOT NULL,
                    sector              TEXT,
                    avg_sentiment_24h   REAL,
                    avg_sentiment_7d    REAL,
                    article_count_24h   INTEGER,
                    price_momentum_5d   REAL,
                    signal              TEXT,
                    confidence          REAL,
                    computed_at         DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sector_signals (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector               TEXT NOT NULL,
                    avg_sentiment_24h    REAL,
                    avg_sentiment_7d     REAL,
                    article_count_24h    INTEGER,
                    avg_price_momentum   REAL,
                    direction            TEXT,
                    confidence           REAL,
                    computed_at          DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS securities (
                    ticker            TEXT PRIMARY KEY,
                    hkex_code         TEXT,
                    name              TEXT NOT NULL,
                    listing_category  TEXT,
                    lot_size          INTEGER,
                    is_watchlist      INTEGER NOT NULL DEFAULT 0,
                    watchlist_sector  TEXT,
                    aliases_json      TEXT,
                    yf_sector         TEXT,
                    yf_industry       TEXT,
                    market            TEXT NOT NULL DEFAULT 'HK',
                    is_active         INTEGER NOT NULL DEFAULT 1,
                    first_seen        DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_refreshed    DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                -- Localised display names + peripheral metadata per ticker.
                -- Kept separate from `securities` so name churn (e.g. an
                -- akshare-sourced Chinese name update) doesn't touch the
                -- core listing record. Will grow additional columns over
                -- time (logo URL, website, headquarters, founded year,
                -- etc.) as more peripheral metadata sources land — that's
                -- why it's named `securities_meta` rather than just
                -- `stock_names`.
                CREATE TABLE IF NOT EXISTS securities_meta (
                    ticker        TEXT PRIMARY KEY,
                    english_name  TEXT,
                    chinese_name  TEXT,
                    updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                -- Cloud-first reference table: bilingual display names +
                -- resolved sector taxonomy per ticker. Source of truth
                -- lives in Supabase (`securities_reference`); this local
                -- mirror serves the dashboard's frequent read path via
                -- the factory router. Same shape as the cloud table.
                -- See storage/cloud_repository.py:CloudSecuritiesReferenceRepository
                -- for the canonical write path; the SQLite copy here is
                -- populated by `python main.py reference refresh`.
                CREATE TABLE IF NOT EXISTS securities_reference (
                    ticker         TEXT PRIMARY KEY,
                    english_name   TEXT,
                    chinese_name   TEXT,
                    parent_sector  TEXT,
                    sub_sector     TEXT,
                    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_securities_reference_parent
                    ON securities_reference (parent_sector);
                CREATE INDEX IF NOT EXISTS idx_securities_reference_sub
                    ON securities_reference (sub_sector);

                -- Tiny denormalised "latest price per ticker" cache.
                -- The canonical source is Supabase `historical_prices` (16M+
                -- rows), but DISTINCT-ON-by-ticker over the cloud pool was
                -- the Screener's main cold-load cost (~40s for ~7k tickers).
                -- This table holds one row per ticker (~7k rows, <200 KB) —
                -- read by every dashboard surface that needs a current
                -- price for filtering/sorting (Screener, Discovery, etc.).
                -- Refreshed nightly by the daily EOD price cron via
                -- `analysis/data_loader.refresh_latest_prices_cache`.
                CREATE TABLE IF NOT EXISTS latest_prices (
                    ticker      TEXT PRIMARY KEY,
                    adj_close   REAL,
                    asof_date   DATE,
                    refreshed_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS fundamentals_snapshots (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker            TEXT NOT NULL,
                    market            TEXT NOT NULL DEFAULT 'HK',
                    snapshot_date     DATE NOT NULL,
                    trailing_pe       REAL,
                    forward_pe        REAL,
                    price_to_book     REAL,
                    ev_to_ebitda      REAL,
                    dividend_yield    REAL,
                    market_cap        REAL,
                    beta              REAL,
                    return_on_equity  REAL,
                    debt_to_equity    REAL,
                    last_price        REAL,
                    currency          TEXT,
                    data_completeness REAL,
                    -- Direction C additions (Stage 1): growth + quality + liquidity
                    earnings_growth   REAL,
                    revenue_growth    REAL,
                    profit_margins    REAL,
                    operating_margins REAL,
                    return_on_assets  REAL,
                    current_ratio     REAL,
                    free_cashflow     REAL,
                    captured_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ticker, snapshot_date)
                );

                CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
                CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker ON article_tickers(ticker);
                CREATE INDEX IF NOT EXISTS idx_sentiment_ticker ON sentiment_scores(ticker, scored_at);
                CREATE INDEX IF NOT EXISTS idx_signals_ticker ON ticker_signals(ticker, computed_at);
                CREATE INDEX IF NOT EXISTS idx_sector_signals ON sector_signals(sector, computed_at);
                CREATE INDEX IF NOT EXISTS idx_securities_watchlist ON securities(is_watchlist);
                CREATE INDEX IF NOT EXISTS idx_securities_category ON securities(listing_category);
                CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker_date ON fundamentals_snapshots(ticker, snapshot_date);
                CREATE INDEX IF NOT EXISTS idx_fundamentals_date ON fundamentals_snapshots(snapshot_date);

                -- Backtest infrastructure (Stage 1 of per-industry optimization)
                CREATE TABLE IF NOT EXISTS historical_prices (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker      TEXT NOT NULL,
                    market      TEXT NOT NULL DEFAULT 'HK',
                    date        DATE NOT NULL,
                    open        REAL,
                    high        REAL,
                    low         REAL,
                    close       REAL,
                    adj_close   REAL,
                    volume      INTEGER,
                    UNIQUE(ticker, date)
                );
                CREATE INDEX IF NOT EXISTS idx_historical_prices_ticker_date
                    ON historical_prices(ticker, date);

                CREATE TABLE IF NOT EXISTS backtest_runs (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id              TEXT UNIQUE NOT NULL,
                    screen_id           TEXT NOT NULL,
                    industry            TEXT,
                    parameters_json     TEXT NOT NULL,
                    start_date          DATE NOT NULL,
                    end_date            DATE NOT NULL,
                    rebalance_freq      TEXT NOT NULL,
                    n_rebalances        INTEGER,
                    total_return        REAL,
                    benchmark_return    REAL,
                    information_ratio   REAL,
                    sharpe              REAL,
                    max_drawdown        REAL,
                    hit_rate            REAL,
                    n_unique_holdings   INTEGER,
                    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_backtest_runs_screen
                    ON backtest_runs(screen_id, industry);

                CREATE TABLE IF NOT EXISTS backtest_holdings (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id          TEXT NOT NULL,
                    rebalance_date  DATE NOT NULL,
                    ticker          TEXT NOT NULL,
                    weight          REAL,
                    return_to_next  REAL,
                    sector          TEXT,
                    UNIQUE(run_id, rebalance_date, ticker)
                );
                CREATE INDEX IF NOT EXISTS idx_backtest_holdings_run
                    ON backtest_holdings(run_id);

                CREATE TABLE IF NOT EXISTS optimized_parameters (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    screen_id               TEXT NOT NULL,
                    industry                TEXT NOT NULL,
                    parameters_json         TEXT NOT NULL,
                    information_ratio       REAL,
                    n_walk_forward_windows  INTEGER,
                    train_window_months     INTEGER,
                    test_window_months      INTEGER,
                    last_optimized_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(screen_id, industry)
                );

                -- Per-ticker research notes (Plain Bagel 6-step framework persistence)
                CREATE TABLE IF NOT EXISTS research_notes (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker              TEXT NOT NULL UNIQUE,
                    research_status     TEXT,             -- raw|researched|watchlist|owned|rejected
                    swot_strengths      TEXT,
                    swot_weaknesses     TEXT,
                    swot_opportunities  TEXT,
                    swot_threats        TEXT,
                    business_notes      TEXT,
                    strategy_notes      TEXT,
                    valuation_notes     TEXT,
                    thesis              TEXT,
                    dcf_inputs_json     TEXT,
                    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_research_notes_status
                    ON research_notes(research_status);

                -- yfinance Ticker.growth_estimates "+5y" stockTrend cache.
                -- Populated lazily by analysis/data_loader.py:get_or_fetch_analyst_growth
                -- and consumed by analysis/dcf.py's 3-tier Y1-5 growth resolver
                -- when no historical CAGR is available. growth_5y may be NULL
                -- when yfinance returns no estimates for the ticker (common for
                -- HK names) — we cache the miss so we don't keep retrying.
                CREATE TABLE IF NOT EXISTS analyst_growth_cache (
                    ticker     TEXT PRIMARY KEY,
                    growth_5y  REAL,            -- fraction, e.g. 0.12 = 12%
                    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Migration: add Direction C columns to fundamentals_snapshots if missing
            # (CREATE TABLE IF NOT EXISTS won't add columns to a pre-existing table).
            self._add_columns_if_missing(conn, "fundamentals_snapshots", [
                ("earnings_growth",   "REAL"),
                ("revenue_growth",    "REAL"),
                ("profit_margins",    "REAL"),
                ("operating_margins", "REAL"),
                ("return_on_assets",  "REAL"),
                ("current_ratio",     "REAL"),
                ("free_cashflow",     "REAL"),
                # Backtest stage 1: per-share metrics needed to compute historical
                # P/E and P/B by combining with historical_prices at backtest time.
                ("eps_ttm",            "REAL"),
                ("bps",                "REAL"),
                ("shares_outstanding", "REAL"),
            ])
            # Sub-sector taxonomy: finer-grained peer grouping than yf_sector.
            # `sub_sector` = the new fine-grained label resolved from
            # config/sub_sectors.yaml. `effective_sector` = parent sector AFTER
            # per-ticker overrides (e.g. BYD's yf_sector stays "Consumer
            # Cyclical" but effective_sector becomes "Technology" so factor
            # scoring buckets it under Tech).
            self._add_columns_if_missing(conn, "securities", [
                ("sub_sector",       "TEXT"),
                ("effective_sector", "TEXT"),
            ])
            # US-market expansion migration: every table that holds per-ticker
            # rows gains a `market` column ('HK' | 'US'). Default 'HK' keeps
            # existing rows unchanged. SQLite's ADD COLUMN can only set a
            # constant default (not an expression), so historical_prices /
            # fundamentals_snapshots get backfilled by ticker convention via
            # _backfill_market_by_ticker() below.
            self._add_columns_if_missing(conn, "articles", [
                ("market", "TEXT NOT NULL DEFAULT 'HK'"),
            ])
            self._add_columns_if_missing(conn, "securities", [
                ("market", "TEXT NOT NULL DEFAULT 'HK'"),
            ])
            self._add_columns_if_missing(conn, "fundamentals_snapshots", [
                ("market", "TEXT NOT NULL DEFAULT 'HK'"),
            ])
            self._add_columns_if_missing(conn, "historical_prices", [
                ("market", "TEXT NOT NULL DEFAULT 'HK'"),
            ])
            # Backfill rows where the convention says US but the column was
            # filled with the 'HK' default during the ADD COLUMN. Safe to
            # re-run; only touches mis-tagged rows.
            self._backfill_market_by_ticker(conn)
            # Loosen the legacy NOT NULL constraint on securities.hkex_code so
            # US rows (which have no HKEX code) can be inserted by Phase 2's
            # reconcile_us(). Idempotent — only rebuilds the table if the
            # legacy constraint is still in place.
            self._drop_hkex_code_not_null(conn)
            # Composite indexes keyed on market — the existing single-column
            # indexes still serve HK-only queries, but cross-market queries
            # benefit from the leading-market column.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_securities_market_active "
                "ON securities(market, is_active)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_market_published "
                "ON articles(market, published_at)"
            )
            conn.commit()
        logger.info("Database initialized at %s", self.db_path)

    def _backfill_market_by_ticker(self, conn):
        """For rows where `market` is the default 'HK' but the ticker
        convention says otherwise, write the correct market. Idempotent —
        safe to re-run; an already-correct row is a no-op."""
        # historical_prices: tickers without `.HK` suffix and not in the
        # HK-indices/composite-prefix set are US.
        conn.execute("""
            UPDATE historical_prices
               SET market = 'US'
             WHERE market = 'HK'
               AND ticker NOT LIKE '%.HK'
               AND ticker NOT IN ('^HSI','^HSCEI','^HSTECH')
               AND ticker NOT LIKE '&HK:%'
               AND ticker NOT LIKE '&%'
               AND ticker NOT LIKE '@%'
        """)
        conn.execute("""
            UPDATE fundamentals_snapshots
               SET market = 'US'
             WHERE market = 'HK'
               AND ticker NOT LIKE '%.HK'
        """)
        conn.commit()

    def _drop_hkex_code_not_null(self, conn):
        """Rebuild `securities` without the NOT NULL constraint on hkex_code
        so US rows can omit it. Idempotent — only runs when the constraint
        is still in place. SQLite has no ALTER COLUMN DROP NOT NULL, so we
        use the standard create-new / copy / drop / rename pattern."""
        # Cheap probe: look at the column definition.
        info = conn.execute("PRAGMA table_info(securities)").fetchall()
        hkex_col = next((r for r in info if r[1] == "hkex_code"), None)
        # PRAGMA columns: (cid, name, type, notnull, dflt_value, pk)
        if hkex_col is None or hkex_col[3] == 0:
            return  # already nullable
        logger.info("Migration: rebuilding securities table to drop hkex_code NOT NULL")
        conn.executescript("""
            BEGIN;
            CREATE TABLE securities_new (
                ticker            TEXT PRIMARY KEY,
                hkex_code         TEXT,
                name              TEXT NOT NULL,
                listing_category  TEXT,
                lot_size          INTEGER,
                is_watchlist      INTEGER NOT NULL DEFAULT 0,
                watchlist_sector  TEXT,
                aliases_json      TEXT,
                yf_sector         TEXT,
                yf_industry       TEXT,
                sub_sector        TEXT,
                effective_sector  TEXT,
                market            TEXT NOT NULL DEFAULT 'HK',
                is_active         INTEGER NOT NULL DEFAULT 1,
                first_seen        DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_refreshed    DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO securities_new (
                ticker, hkex_code, name, listing_category, lot_size,
                is_watchlist, watchlist_sector, aliases_json,
                yf_sector, yf_industry, sub_sector, effective_sector,
                market, is_active, first_seen, last_refreshed
            )
            SELECT
                ticker, hkex_code, name, listing_category, lot_size,
                is_watchlist, watchlist_sector, aliases_json,
                yf_sector, yf_industry, sub_sector, effective_sector,
                market, is_active, first_seen, last_refreshed
            FROM securities;
            DROP TABLE securities;
            ALTER TABLE securities_new RENAME TO securities;
            CREATE INDEX IF NOT EXISTS idx_securities_watchlist ON securities(is_watchlist);
            CREATE INDEX IF NOT EXISTS idx_securities_category  ON securities(listing_category);
            CREATE INDEX IF NOT EXISTS idx_securities_market_active ON securities(market, is_active);
            COMMIT;
        """)

    def _add_columns_if_missing(self, conn, table: str, columns: list[tuple[str, str]]):
        """Idempotently add columns; SQLite has no IF NOT EXISTS for ADD COLUMN."""
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_type in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                logger.info("Migration: added %s.%s", table, col_name)
        conn.commit()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn
