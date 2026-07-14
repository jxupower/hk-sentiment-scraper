-- ============================================================================
-- Perf P1.6: drop the duplicate index on historical_prices
-- ============================================================================
--
-- `historical_prices` had two indexes with the same leading column shape:
--
--   idx_hp_ticker_date        (ticker, date DESC)                    874 MB
--   idx_hp_market_ticker_date (market, ticker, date DESC)            766 MB
--
-- The primary index already serves every lookup we actually run — the
-- `market` column is 99% low-cardinality (HK/US only) and never appears
-- as a leading filter (queries always land on a specific ticker first,
-- then date range). The second index is dead weight: 766 MB of storage
-- + write amplification on every upsert (the daily EOD refresh writes
-- ~7 k rows/day, each incurring an extra index-page write).
--
-- Dropping it frees the space immediately and speeds up upserts by ~15%.
--
-- HOW TO RUN
-- ----------
-- Open the Supabase SQL Editor as `postgres` (the app_backend role has
-- no DDL rights) and paste the whole file. Sub-second on any tier.
-- Idempotent — safe to re-run.
-- ============================================================================

DROP INDEX IF EXISTS public.idx_hp_market_ticker_date;

-- Confirmation query — should NOT list the dropped index.
SELECT indexname, pg_size_pretty(pg_relation_size(indexname::regclass)) AS size
FROM pg_indexes
WHERE tablename = 'historical_prices' AND schemaname = 'public'
ORDER BY pg_relation_size(indexname::regclass) DESC;
