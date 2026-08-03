-- ============================================================================
-- Supabase free-tier cleanup — consolidated F2 + F3 (2026-07-30)
-- ============================================================================
--
-- Cloud DB is currently 3974 MB (~8x the 500 MB free-tier ceiling). Two
-- reclaimable stanzas below:
--
--   STANZA A (F3): DROP redundant index idx_hp_market_ticker_date
--       Reclaims ~766 MB. Safe to run anytime — no application impact.
--
--   STANZA B (F2): DROP TABLE historical_prices
--       Reclaims ~3.9 GB. GATED — only run AFTER:
--         1. scripts/seed_parquet_from_supabase.py has completed
--         2. scripts/smoke_test_parquet_reads.py exits with code 0
--         3. dashboard tabs (Portfolio / Risk Forecast / Stock Research)
--            confirmed working against local Parquet
--
-- HOW TO RUN
-- ----------
-- Open the Supabase SQL Editor as `postgres` (the app_backend role
-- does not have DDL rights). Copy-paste each stanza in turn — the
-- SQL Editor already auto-commits each statement so no BEGIN/COMMIT
-- wrapping is needed.
--
-- Idempotent — safe to re-run each stanza; a missing object is a no-op.
-- ============================================================================


-- ============================================================================
-- STANZA A (F3): drop redundant index on historical_prices
-- ============================================================================
--
-- `historical_prices` currently has 3 indexes:
--
--   historical_prices_pkey        UNIQUE (ticker, date)             620 MB
--   idx_hp_ticker_date            (ticker, date DESC)                874 MB
--   idx_hp_market_ticker_date     (market, ticker, date DESC)        766 MB  <-- REDUNDANT
--
-- The `market` column is 99% low-cardinality (HK/US only) and no query
-- ever filters by market first — every access starts with a specific
-- ticker. So the third index is 766 MB of dead weight + write-
-- amplification on every EOD upsert (~7 k rows/day, each extra index
-- page write for nothing).
--
-- NOTE: the Supabase SQL Editor wraps every submission in a transaction
-- block, so DROP INDEX CONCURRENTLY errors out ("25001: cannot run
-- inside a transaction block"). Plain DROP INDEX takes an ACCESS
-- EXCLUSIVE lock, but the operation is metadata-only and completes in
-- <1s — safe here because no query in the codebase filters
-- historical_prices by `market` first (every path lands on a specific
-- ticker), and any brief blip is harmless because writes retry.

DROP INDEX IF EXISTS public.idx_hp_market_ticker_date;

-- Confirm — should list only 2 indexes now.
SELECT indexname,
       pg_size_pretty(pg_relation_size(quote_ident(indexname)::regclass)) AS size
FROM pg_indexes
WHERE tablename = 'historical_prices' AND schemaname = 'public'
ORDER BY pg_relation_size(quote_ident(indexname)::regclass) DESC;

-- Total DB size after Stanza A (expected: ~3208 MB, down from 3974).
SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size_after_A;


-- ============================================================================
-- STANZA B (F2): drop historical_prices table entirely
-- ============================================================================
--
-- ONLY RUN THIS AFTER:
--
--   [ ] `python scripts/seed_parquet_from_supabase.py` completed
--       (target: data/prices/ populated with all ~7 k tickers)
--
--   [ ] `python scripts/smoke_test_parquet_reads.py` exits 0
--       (verifies row-count parity, latest-date parity, and deep row
--        diff on 15 random tickers)
--
--   [ ] Dashboard tabs Portfolio, Risk Forecast, Stock Research all
--       load and render correctly with USE_PARQUET_PRICES unset (the
--       factory auto-flips because store_populated() is True).
--
--   [ ] You are comfortable losing the cloud copy — the only backup
--       will be the local Parquet files at data/prices/. Consider
--       tarballing data/prices/ before running this.
--
-- To arm the drop, delete the leading "-- " on the two DROP lines below
-- and uncomment the confirmation SELECT. Then paste into SQL Editor.
--
-- Reclaims ~3.94 GB (or ~3.17 GB if Stanza A ran first). DB drops
-- from ~3208 MB to ~30 MB. Free-tier ceiling relief confirmed.

-- DROP TABLE IF EXISTS public.historical_prices;

-- After the drop, verify:
-- SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size_after_B;
-- SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;


-- ============================================================================
-- ROLLBACK / KILL-SWITCH
-- ============================================================================
--
-- If Stanza B was run in error, historical_prices can be re-seeded to
-- Supabase from the local Parquet store:
--
--   1. Re-create the table by pasting the historical_prices DDL from
--      scripts/supabase_schema.sql (lines 20-63).
--   2. Run a reverse-direction bulk copy script (not currently included
--      because rollback expected to be extremely rare) or use the
--      existing bulk seeders in scripts/resume_historical_seed.py after
--      switching them from yfinance to the Parquet store.
--
-- If Stanza A was run in error, re-create the index (~15 min for
-- 17M rows) via `CREATE INDEX CONCURRENTLY`:
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_hp_market_ticker_date
--     ON public.historical_prices (market, ticker, date DESC);
--
-- No application will notice the outage — the primary key + the other
-- ticker,date index cover every query path.
