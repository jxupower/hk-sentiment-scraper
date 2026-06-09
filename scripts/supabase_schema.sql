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

CREATE TABLE IF NOT EXISTS historical_prices (
    ticker        TEXT          NOT NULL,
    date          DATE          NOT NULL,
    open          NUMERIC(12, 4),
    high          NUMERIC(12, 4),
    low           NUMERIC(12, 4),
    close         NUMERIC(12, 4),
    adj_close     NUMERIC(12, 4),
    volume        BIGINT,
    fetched_at    TIMESTAMPTZ   DEFAULT NOW(),
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_hp_ticker_date
    ON historical_prices (ticker, date DESC);

-- ============== fundamentals_snapshots ==============

CREATE TABLE IF NOT EXISTS fundamentals_snapshots (
    ticker             TEXT          NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_fs_ticker_date
    ON fundamentals_snapshots (ticker, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_fs_source
    ON fundamentals_snapshots (source);

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

-- ============== Smoke-test seed (delete after verifying) ==============
-- INSERT INTO historical_prices (ticker, date, adj_close)
--   VALUES ('TEST.HK', CURRENT_DATE, 100.00)
--   ON CONFLICT (ticker, date) DO NOTHING;
-- SELECT * FROM historical_prices WHERE ticker = 'TEST.HK';
