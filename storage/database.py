import atexit
import sqlite3
import threading
from pathlib import Path
from utils.logger import get_logger

logger = get_logger(__name__)


# Module-level thread-local cache: {db_path: connection} per thread.
# Callers frequently do `Database(db_path).get_connection()` inline, so a
# per-instance cache doesn't help (each Database() gets a fresh
# threading.local). A module-level thread-local keyed by db_path shares
# across all Database instances constructed by the same thread — which is
# the actual hot pattern in dashboard callbacks.
_thread_conns = threading.local()


def _thread_conn_map() -> dict[str, sqlite3.Connection]:
    m = getattr(_thread_conns, "map", None)
    if m is None:
        m = {}
        _thread_conns.map = m
    return m


def _shutdown_thread_conns() -> None:
    """Close any cached connections held by the calling thread at process
    exit. Only closes connections in the exiting thread's local state —
    other threads' cached connections are released by the OS when the
    process terminates."""
    m = getattr(_thread_conns, "map", None)
    if m:
        for c in m.values():
            try:
                c.close()
            except Exception:
                pass
        m.clear()


atexit.register(_shutdown_thread_conns)


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
                    market              TEXT NOT NULL DEFAULT 'HK',
                    avg_sentiment_24h   REAL,
                    avg_sentiment_7d    REAL,
                    article_count_24h   INTEGER,
                    article_count_7d    INTEGER,
                    price_momentum_5d   REAL,
                    signal              TEXT,
                    confidence          REAL,
                    computed_at         DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sector_signals (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector               TEXT NOT NULL,
                    market               TEXT NOT NULL DEFAULT 'HK',
                    avg_sentiment_24h    REAL,
                    avg_sentiment_7d     REAL,
                    article_count_24h    INTEGER,
                    article_count_7d     INTEGER,
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
                    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                    -- Stock Research tab AI-generated statement cache.
                    -- Written on button click, read on report load.
                    -- Mirrors scripts/supabase_schema.sql. Reconciler
                    -- upsert_many enumerates only the 5 reference columns
                    -- so the ai_* columns survive its writes.
                    ai_business_summary    TEXT,
                    ai_business_summary_at DATETIME,
                    ai_forensic_review     TEXT,
                    ai_forensic_review_at  DATETIME,
                    ai_bull_bear           TEXT,
                    ai_bull_bear_at        DATETIME,
                    ai_devil_advocate      TEXT,
                    ai_devil_advocate_at   DATETIME
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

                -- Compiled taxonomy: parents + sub-sectors with bilingual labels.
                -- Written by analysis.taxonomy.compile_taxonomy(); read by
                -- get_taxonomy() at runtime. Source of truth lives in
                -- config/sub_sectors.yaml et al.
                CREATE TABLE IF NOT EXISTS sector_taxonomy (
                    canonical_name TEXT PRIMARY KEY,
                    kind           TEXT NOT NULL,                 -- 'parent' | 'sub'
                    parent_name    TEXT,                          -- NULL for parents
                    label_en       TEXT NOT NULL,
                    label_zh       TEXT NOT NULL,
                    display_order  INTEGER NOT NULL DEFAULT 999,
                    is_active      INTEGER NOT NULL DEFAULT 1,    -- BOOLEAN as INTEGER
                    compiled_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (kind IN ('parent', 'sub'))
                );
                CREATE INDEX IF NOT EXISTS idx_sector_taxonomy_parent
                    ON sector_taxonomy(parent_name);

                -- Single-row key/value table for the compile version hash
                -- (used as cache-bust key by the runtime Taxonomy singleton).
                CREATE TABLE IF NOT EXISTS taxonomy_meta (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                -- Reclassification audit. Reconciler writes one row per
                -- ticker only when (sub_sector, effective_sector) changes.
                CREATE TABLE IF NOT EXISTS ticker_taxonomy_history (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker               TEXT NOT NULL,
                    sub_sector           TEXT,
                    effective_sector     TEXT,
                    market_cap_at_change REAL,
                    reason               TEXT NOT NULL,
                    changed_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_tth_ticker_time
                    ON ticker_taxonomy_history(ticker, changed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tth_reason
                    ON ticker_taxonomy_history(reason, changed_at DESC);
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
            # sector_signals + ticker_signals get a `market` column so HK/US
            # scrapes don't overwrite each other's rows for shared sub-sectors
            # (Semiconductors & Equipment, Banks, etc.). Existing rows are
            # tagged 'HK' by the default — they get replaced by the next
            # scrape cycle anyway (signals are refreshed every 30 min).
            self._add_columns_if_missing(conn, "sector_signals", [
                ("market", "TEXT NOT NULL DEFAULT 'HK'"),
                # 7-day companion metrics: added to power the Sentiment tab
                # card badge (24h alone is too tight — many sub-sectors get 0
                # articles per day even when there are dozens per week). The
                # card reads `article_count_7d`; direction/confidence stay
                # driven by the fresh 24h aggregate so the signal is not stale.
                ("article_count_7d", "INTEGER"),
                # avg_sentiment_7d already existed in the schema but was
                # always NULL — the job runner now fills it too.
            ])
            self._add_columns_if_missing(conn, "ticker_signals", [
                ("market", "TEXT NOT NULL DEFAULT 'HK'"),
                ("article_count_7d", "INTEGER"),
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
            # Idempotent add of the AI-cache columns on securities_reference
            # so pre-existing local databases pick them up without a rebuild.
            # SQLite doesn't support ADD COLUMN IF NOT EXISTS natively; we
            # gate on PRAGMA table_info instead. Cheap — runs once per boot
            # on a table with 8 columns to check.
            self._add_ai_cache_columns_if_missing(conn)
            conn.commit()
        logger.info("Database initialized at %s", self.db_path)

    def _add_ai_cache_columns_if_missing(self, conn):
        """Idempotent ALTER TABLE ADD COLUMN for the 8 Stock-Research AI
        cache columns. Uses PRAGMA table_info to check for existence —
        SQLite doesn't offer ADD COLUMN IF NOT EXISTS."""
        cur = conn.execute("PRAGMA table_info(securities_reference)")
        existing = {row[1] for row in cur.fetchall()}
        ai_columns = [
            ("ai_business_summary",    "TEXT"),
            ("ai_business_summary_at", "DATETIME"),
            ("ai_forensic_review",     "TEXT"),
            ("ai_forensic_review_at",  "DATETIME"),
            ("ai_bull_bear",           "TEXT"),
            ("ai_bull_bear_at",        "DATETIME"),
            ("ai_devil_advocate",      "TEXT"),
            ("ai_devil_advocate_at",   "DATETIME"),
        ]
        for name, dtype in ai_columns:
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE securities_reference ADD COLUMN {name} {dtype}"
                )

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
        """Return a SQLite connection cached per (thread, db_path).

        Measured on the current DB: a fresh sqlite3.connect + PRAGMA replay
        costs ~0.63 ms; a cached connection is ~0 ms. A cold dashboard `/`
        load touches ~76 call sites, so this saves ~50 ms of critical path.

        Callers keep the same `with self.db.get_connection() as conn:`
        pattern — sqlite3.Connection.__exit__ commits/rolls-back the
        current transaction on scope exit but does NOT close the
        connection, so a cached conn behaves identically to a fresh one
        across sequential `with` blocks. The cache is per-thread (SQLite
        connections are single-thread by default), and released on
        process exit via `atexit`.
        """
        m = _thread_conn_map()
        conn = m.get(self.db_path)
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            m[self.db_path] = conn
        return conn
