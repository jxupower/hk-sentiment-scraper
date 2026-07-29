-- Migration: add AI-generated statement cache to securities_reference.
--
-- Adds 8 columns (4 TEXT + 4 TIMESTAMPTZ) that store the Stock Research
-- tab's Claude-powered outputs so they survive across dashboard reloads
-- and don't need to be regenerated (Claude spend + wall-clock cost) on
-- every viewing.
--
-- Written on button click by dashboard.stock_research_callbacks; read on
-- report load by the same module's render_report callback. The four
-- (kind, kind_at) pairs correspond one-to-one with the four AI sections:
--   Section 2   business summary
--   Section 3b  forensic review + bull/bear stress test
--   Section 6   devil's advocate
--
-- All eight are IF NOT EXISTS so re-running the migration is a no-op.
-- Applies to both a fresh Supabase project and one that was created
-- before this feature landed.

SET lock_timeout = '10s';
SET statement_timeout = '60s';

ALTER TABLE securities_reference
    ADD COLUMN IF NOT EXISTS ai_business_summary    TEXT,
    ADD COLUMN IF NOT EXISTS ai_business_summary_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ai_forensic_review     TEXT,
    ADD COLUMN IF NOT EXISTS ai_forensic_review_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ai_bull_bear           TEXT,
    ADD COLUMN IF NOT EXISTS ai_bull_bear_at        TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ai_devil_advocate      TEXT,
    ADD COLUMN IF NOT EXISTS ai_devil_advocate_at   TIMESTAMPTZ;

-- Sanity check — should print 8 rows, one per new column, all with
-- data_type either 'text' or 'timestamp with time zone'.
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'securities_reference'
  AND column_name LIKE 'ai_%'
ORDER BY column_name;
