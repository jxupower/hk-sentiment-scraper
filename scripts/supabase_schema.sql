-- Supabase Postgres schema for HK Sentiment Scraper cloud DB.
-- Paste this into your Supabase project's SQL Editor and Run.
-- Idempotent — safe to re-run; only creates if missing.
--
-- Tables hosted in cloud:
--   historical_prices     - daily OHLCV per ticker
--   fundamentals_snapshots - annual akshare fundamentals + (eventually) on-demand yfinance .info
--
-- Everything else (articles, sentiment, signals, securities, research_notes,
-- backtest_*) stays in local SQLite — see CLAUDE.md.

-- ============== historical_prices ==============

-- NUMERIC(14,4): max ~10^10 = $10B per share. Stretches to cover
-- pathological backward-split-adjusted prices on US micro-caps (a stock
-- doing a 1:1000 reverse split causes its yfinance `adj_close` to
-- spike historically into the 10^7+ range). HK names + indices fit
-- comfortably in (12,4) but the wider type costs ~0 storage and
-- removes a class of silent-failure cases.
CREATE TABLE IF NOT EXISTS historical_prices (
    ticker        TEXT          NOT NULL,
    market        TEXT          NOT NULL DEFAULT 'HK',
    date          DATE          NOT NULL,
    open          NUMERIC(14, 4),
    high          NUMERIC(14, 4),
    low           NUMERIC(14, 4),
    close         NUMERIC(14, 4),
    adj_close     NUMERIC(14, 4),
    volume        BIGINT,
    fetched_at    TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (ticker, date)
);

-- Idempotent column widening for existing deployments.
ALTER TABLE historical_prices
    ALTER COLUMN open      TYPE NUMERIC(14, 4),
    ALTER COLUMN high      TYPE NUMERIC(14, 4),
    ALTER COLUMN low       TYPE NUMERIC(14, 4),
    ALTER COLUMN close     TYPE NUMERIC(14, 4),
    ALTER COLUMN adj_close TYPE NUMERIC(14, 4);

-- Idempotent: add `market` to pre-existing deployments.
ALTER TABLE historical_prices
    ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'HK';

-- Backfill any row whose ticker convention says US (one-shot for legacy data).
UPDATE historical_prices
   SET market = 'US'
 WHERE market = 'HK'
   AND ticker NOT LIKE '%.HK'
   AND ticker NOT IN ('^HSI','^HSCEI','^HSTECH')
   AND ticker NOT LIKE '&HK:%'
   AND ticker NOT LIKE '&%'
   AND ticker NOT LIKE '@%';

CREATE INDEX IF NOT EXISTS idx_hp_ticker_date
    ON historical_prices (ticker, date DESC);

-- REMOVED (perf P1.6, 2026-07-14): `idx_hp_market_ticker_date` duplicated
-- the leading-column shape of `idx_hp_ticker_date`. Queries always land on
-- a specific ticker first (market is 99% HK/US low-cardinality and never
-- appears as a leading filter), so the second index was 766 MB of dead
-- weight + write amplification. See scripts/supabase_drop_duplicate_hp_index.sql.

-- ============== fundamentals_snapshots ==============

CREATE TABLE IF NOT EXISTS fundamentals_snapshots (
    ticker             TEXT          NOT NULL,
    market             TEXT          NOT NULL DEFAULT 'HK',
    snapshot_date      DATE          NOT NULL,
    source             TEXT          NOT NULL DEFAULT 'akshare_annual',
    -- Per-share / shares
    eps_ttm            NUMERIC,
    bps                NUMERIC,
    shares_outstanding NUMERIC,
    -- Valuation
    market_cap         NUMERIC,
    trailing_pe        NUMERIC,
    forward_pe         NUMERIC,
    price_to_book      NUMERIC,
    ev_to_ebitda       NUMERIC,
    dividend_yield     NUMERIC,
    -- Quality / profitability
    return_on_equity   NUMERIC,
    return_on_assets   NUMERIC,
    profit_margins     NUMERIC,
    operating_margins  NUMERIC,
    debt_to_equity     NUMERIC,
    current_ratio      NUMERIC,
    -- Growth
    earnings_growth    NUMERIC,
    revenue_growth     NUMERIC,
    -- Cashflow / liquidity
    free_cashflow      NUMERIC,
    -- Misc
    beta               NUMERIC,
    last_price         NUMERIC,
    currency           TEXT,
    data_completeness  NUMERIC,
    fetched_at         TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (ticker, snapshot_date, source)
);

-- Idempotent: add `market` to pre-existing deployments + backfill by convention.
ALTER TABLE fundamentals_snapshots
    ADD COLUMN IF NOT EXISTS market TEXT NOT NULL DEFAULT 'HK';

UPDATE fundamentals_snapshots
   SET market = 'US'
 WHERE market = 'HK'
   AND ticker NOT LIKE '%.HK';

CREATE INDEX IF NOT EXISTS idx_fs_ticker_date
    ON fundamentals_snapshots (ticker, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_fs_source
    ON fundamentals_snapshots (source);

CREATE INDEX IF NOT EXISTS idx_fs_market_ticker
    ON fundamentals_snapshots (market, ticker);

-- ============== financial_statements ==============
-- Raw filings: income statement, balance sheet, cash flow per period.
-- JSONB blob per (ticker, statement_type, period_end_date) so we don't have to
-- declare ~50 line-item columns per statement type. Line-item names vary
-- between yfinance (English) and akshare (Chinese) so a fixed schema would
-- be either huge (50+ NULL-able cols) or lossy. Cache-aside only — populated
-- on first Research-tab visit to a ticker.

CREATE TABLE IF NOT EXISTS financial_statements (
    ticker           TEXT          NOT NULL,
    statement_type   TEXT          NOT NULL,  -- 'income' | 'balance' | 'cashflow'
    period_end_date  DATE          NOT NULL,
    period_type      TEXT          NOT NULL,  -- 'annual' | 'semiannual' | 'quarterly'
    source           TEXT          NOT NULL,  -- 'yfinance' | 'akshare'
    currency         TEXT,                    -- 'HKD' | 'CNY' | 'USD' etc.
    line_items       JSONB         NOT NULL,  -- {"Total Revenue": 12345.67, ...}
    fetched_at       TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (ticker, statement_type, period_end_date, period_type)
);

CREATE INDEX IF NOT EXISTS idx_fs_ticker_type
    ON financial_statements (ticker, statement_type, period_end_date DESC);

-- ============== portfolios ==============
-- User-saved portfolios. Each row stores BOTH the raw holdings (ticker, shares)
-- and an optional snapshot of optimal weights from the Portfolio tab's
-- max-Sharpe solve. The dashboard then materialises two synthetic tickers
-- per portfolio into historical_prices:
--    @NAME       -- status-quo (constant-share buy-and-hold) index
--    @NAME$OPT   -- max-Sharpe optimal-weight index (only if optimal_weights set)
-- Risk Forecast and any other tab that reads historical_prices can then
-- consume them like any normal ticker. Name is enforced uppercase alphanumeric
-- in application code (the @-prefix convention is added on read).

CREATE TABLE IF NOT EXISTS portfolios (
    name             TEXT          PRIMARY KEY,
    holdings         JSONB         NOT NULL,           -- [{ticker, shares}, ...]
    optimal_weights  JSONB,                            -- [{ticker, weight}, ...] or NULL
    rf               NUMERIC       DEFAULT 0,          -- rf used when computing optimal_weights
    weight_cap       NUMERIC,                          -- cap used when computing optimal_weights
    lookback_days    INTEGER,                          -- lookback used when computing optimal_weights
    notes            TEXT,
    created_at       TIMESTAMPTZ   DEFAULT NOW(),
    updated_at       TIMESTAMPTZ   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_portfolios_updated
    ON portfolios (updated_at DESC);

-- ============== securities_reference ==============
-- Per-ticker reference rows: bilingual display names + the resolved sector
-- taxonomy (parent_sector, sub_sector). Cloud-first since the dashboard
-- reads these on every Screener / Discovery / Stock Research render, and
-- the local SQLite mirror works as a read-through cache via the factory
-- routing in storage/factory.py.
--
-- Single source of truth for name + sector display data. Other code paths
-- (resolver in universe/reconciler.py, watchlist YAML, us_sectors.yaml)
-- write INTO this table during `universe-us seed` / `universe refresh`,
-- never read from it.
--
-- Estimated size: ~7k rows × ~230 B (incl. 2 secondary indexes) ≈ 1.6 MB.
-- See plan in C:\Users\User\.claude\plans\wobbly-bouncing-spindle.md.

CREATE TABLE IF NOT EXISTS securities_reference (
    ticker         TEXT        PRIMARY KEY,
    english_name   TEXT,
    chinese_name   TEXT,
    parent_sector  TEXT,
    sub_sector     TEXT,
    updated_at     TIMESTAMPTZ DEFAULT NOW(),
    -- AI-generated statement cache for the Stock Research tab. Four
    -- (text, timestamp) pairs, one per Claude-powered section. Written
    -- on button click, read on report load. See
    -- scripts/supabase_add_ai_columns.sql for the migration applied to
    -- pre-existing databases.
    -- CRITICAL: the reconciler's upsert_many enumerates ONLY the 5
    -- reference columns (english_name, chinese_name, parent_sector,
    -- sub_sector, updated_at) in its ON CONFLICT DO UPDATE — do not
    -- change that shape without also excluding the ai_* columns, or
    -- every reconciler run will erase the cache.
    ai_business_summary    TEXT,
    ai_business_summary_at TIMESTAMPTZ,
    ai_forensic_review     TEXT,
    ai_forensic_review_at  TIMESTAMPTZ,
    ai_bull_bear           TEXT,
    ai_bull_bear_at        TIMESTAMPTZ,
    ai_devil_advocate      TEXT,
    ai_devil_advocate_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_securities_reference_parent
    ON securities_reference (parent_sector);
CREATE INDEX IF NOT EXISTS idx_securities_reference_sub
    ON securities_reference (sub_sector);

-- ============== sector_taxonomy ==============
-- Compiled taxonomy: one row per parent sector or sub-sector. Source of
-- TRUTH stays in config/sub_sectors.yaml + config/us_size_splits.yaml +
-- watchlist YAMLs; this table is the validated, normalised output that
-- the runtime reads. Refreshed by `python main.py taxonomy compile`.
--
-- ~104 rows total: 11 parent sectors + ~93 sub-sectors. Loaded once into
-- process memory via analysis/taxonomy.get_taxonomy() and re-checked via
-- taxonomy_meta.version on a 5-min TTL.

CREATE TABLE IF NOT EXISTS sector_taxonomy (
    canonical_name   TEXT          PRIMARY KEY,
    kind             TEXT          NOT NULL,    -- 'parent' | 'sub'
    parent_name      TEXT,                       -- NULL for parents; canonical_name FK for subs
    label_en         TEXT          NOT NULL,
    label_zh         TEXT          NOT NULL,
    display_order    INTEGER       NOT NULL DEFAULT 999,
    is_active        BOOLEAN       NOT NULL DEFAULT TRUE,
    compiled_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    CHECK (kind IN ('parent', 'sub'))
);

CREATE INDEX IF NOT EXISTS idx_sector_taxonomy_parent
    ON sector_taxonomy (parent_name);

-- ============== taxonomy_meta ==============
-- Single-row table for the compile version hash. Used by the runtime
-- singleton (analysis/taxonomy.get_taxonomy) to detect when in-process
-- caches need to invalidate after a `taxonomy compile` rerun.

CREATE TABLE IF NOT EXISTS taxonomy_meta (
    key          TEXT          PRIMARY KEY,
    value        TEXT          NOT NULL,
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ============== ticker_taxonomy_history ==============
-- Audit trail for ticker reclassifications. The reconciler appends a row
-- only when (sub_sector, effective_sector) actually changes vs the latest
-- existing row for that ticker. Bounded growth: ~10-100 rows/reconcile.
-- Reasons: 'initial' | 'industry_change' | 'override_added' |
--          'size_split' | 'override_removed'

CREATE TABLE IF NOT EXISTS ticker_taxonomy_history (
    id                    BIGSERIAL    PRIMARY KEY,
    ticker                TEXT         NOT NULL,
    sub_sector            TEXT,
    effective_sector      TEXT,
    market_cap_at_change  NUMERIC,
    reason                TEXT         NOT NULL,
    changed_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tth_ticker_time
    ON ticker_taxonomy_history (ticker, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_tth_reason
    ON ticker_taxonomy_history (reason, changed_at DESC);

-- ============== Smoke-test seed (delete after verifying) ==============
-- INSERT INTO historical_prices (ticker, date, adj_close)
--   VALUES ('TEST.HK', CURRENT_DATE, 100.00)
--   ON CONFLICT (ticker, date) DO NOTHING;
-- SELECT * FROM historical_prices WHERE ticker = 'TEST.HK';
