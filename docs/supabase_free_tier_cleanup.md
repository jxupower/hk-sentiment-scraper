# Supabase free-tier cleanup runbook

Reclaims ~4.7 GB from the cloud DB (currently 3974 MB, 8× the 500 MB free-tier ceiling). Closes health-check faults F2, F3, and F4.

**Time budget:** ~30–60 min operator wall clock. The Parquet seed is the long pole (~15–30 min for ~7,000 tickers on a 6-worker pool).

**Prerequisite:** `.env` has `SUPABASE_DB_URL` pointing at the session-pooler URI you already use.

---

## What each fault is

| Fault | What | Reclaims | Risk |
|---|---|---|---|
| **F2** | Drop cloud `historical_prices` table (was migrated to local Parquet — migration abandoned mid-way) | ~3.94 GB | HIGH until Parquet seeded + smoke-tested |
| **F3** | Drop duplicate index `idx_hp_market_ticker_date` (redundant with `idx_hp_ticker_date`) | ~766 MB | LOW — safe anytime |
| **F4** | 3 taxonomy tables in `supabase_schema.sql` never actually mirrored to cloud (SQLite-only by design) | 0 | none — documentation only |

---

## Order of operations

Execute in this order. F3 first (safe), then F2 gated on Parquet seed passing smoke tests. F4 is already fixed by an inline banner in `scripts/supabase_schema.sql` — no operator action required.

```
  [1] F3 — Drop duplicate index (SQL Editor, ~5 s)
       ↓
  [2] F2a — Bulk-seed Parquet from Supabase (Python, ~15-30 min)
       ↓
  [3] F2b — Run smoke tests (Python, ~1-2 min)
       ↓
  [4] F2c — Dashboard sanity check (browser, ~5 min)
       ↓
  [5] F2d — Drop cloud table (SQL Editor, ~15 s)
       ↓
  [6] Post-cleanup verification (SQL Editor, ~5 s)
```

---

## [1] F3 — Drop duplicate index

**Where:** Supabase SQL Editor (runs as `postgres`; `app_backend` lacks DDL rights).

**What:** paste **Stanza A** from [scripts/supabase_free_tier_cleanup.sql](../scripts/supabase_free_tier_cleanup.sql). It runs:

```sql
DROP INDEX IF EXISTS public.idx_hp_market_ticker_date;
```

Plain `DROP INDEX` (not `CONCURRENTLY`) because the Supabase SQL Editor wraps every submission in a transaction block, which is incompatible with `CONCURRENTLY`. The drop takes an `ACCESS EXCLUSIVE` lock but the operation is metadata-only — completes in <1s. No app path filters by `market` first, so no query is affected.

**Expected result:**
- Index list drops from 3 → 2 (only `historical_prices_pkey` + `idx_hp_ticker_date` remain).
- DB size goes from 3974 MB → ~3208 MB.

**Rollback** (only if you regret it — no app impact reported): `CREATE INDEX CONCURRENTLY idx_hp_market_ticker_date ON historical_prices (market, ticker, date DESC);` (~15 min for 17 M rows).

---

## [2] F2a — Bulk-seed Parquet from Supabase

**Where:** local terminal, venv activated.

**Command:**

```bash
venv\Scripts\activate
python scripts/seed_parquet_from_supabase.py
```

**What it does:**

1. Queries Supabase for a full ticker inventory (`ticker → row_count, latest_date`).
2. For each of ~7,063 tickers, checks whether local Parquet is already up-to-date:
   - `local_latest >= cloud_latest` AND `local_count >= cloud_count` → **skip**
   - `local_latest` set but older → **incremental** (fetch only newer rows)
   - No local data → **full seed**
3. Writes go to `data/prices/<TICKER>/year=<YEAR>.parquet` (Hive-partitioned, snappy-compressed).
4. Progress line per ticker: `[N/M] TICKER  status  +rows  Xs  (rate  ETA)`.
5. Checkpoint at `data/.parquet_seed_checkpoint.json` — persists every 50 tickers, safe to interrupt with Ctrl-C and resume.

**Options:**

```bash
# Dry-test on 20 tickers first
python scripts/seed_parquet_from_supabase.py --limit 20

# Re-seed specific tickers from scratch (ignore checkpoint)
python scripts/seed_parquet_from_supabase.py --force 0700.HK 6181.HK

# Lower concurrency if the pooler complains
python scripts/seed_parquet_from_supabase.py --workers 4
```

**Expected wall clock:** 15–30 min at 6 workers over the session pooler. Median ticker: ~0.5–1.5 s.

**Expected disk:** ~600 MB to 1.2 GB in `data/prices/` (snappy Parquet is ~5–6× smaller than the raw NUMERIC(14,4) in Postgres — hence why the local store fits comfortably even though the cloud table is 3.9 GB).

**Errors:** any ticker that errors is logged and counted in the summary; script exits `1`. Re-run (idempotent) to retry.

---

## [3] F2b — Run smoke tests

**Where:** local terminal.

**Command:**

```bash
python scripts/smoke_test_parquet_reads.py
```

**What it checks:**

1. `store_populated()` returns True (proves ≥100 ticker dirs exist).
2. `storage.factory.get_prices_repo()` now returns `ParquetHistoricalPricesRepository` — the automatic flip took effect.
3. **Ticker inventory parity:** every ticker in Supabase is present in Parquet.
4. **Per-ticker row-count parity** (tolerance configurable, default 0).
5. **Per-ticker latest_date parity** (Parquet must be ≥ cloud).
6. **Deep row parity:** 15 random tickers × last 30 rows compared field-by-field. `adj_close` diffs > 1e-4 flagged.
7. **Live caller smoke test:** `analysis.data_loader.get_or_fetch_prices` returns non-empty series for 6 watchlist tickers (Tencent / BABA / Laopu / HSBC / AAPL / ^HSI).

**Exit codes:**
- `0` = all pass → safe to proceed to step 4.
- `1` = at least one FAIL → **stop**. Re-run the seed script to close the gap, then re-run this smoke test. Do NOT proceed to step 5.

**Tuning:**

```bash
# More paranoid — 50 tickers deep-checked, tolerance 0
python scripts/smoke_test_parquet_reads.py --sample 50 --tolerance 0

# Allow floating-point rounding fuzz (1 cent)
python scripts/smoke_test_parquet_reads.py --tolerance 0.01
```

---

## [4] F2c — Dashboard sanity check

Not automated — needs a real browser session with a human eye.

```bash
python main.py dashboard
```

Open http://localhost:8050 and click through:

- [ ] **Stock Research** — load Tencent (`0700.HK`). Section 4 charts must render; DCF sliders must respond. Load Laopu (`6181.HK`) — P/E should show ~9.3 (the live-price patched value), not NULL.
- [ ] **Portfolio** — click Optimize on any saved portfolio (or add a couple of holdings and hit Optimize). Efficient-frontier chart must render, not spin forever.
- [ ] **Risk Forecast** — pick `^HSI` and any HK ticker; run the 5-day forecast. Fan chart + VaR table must populate within ~5 s.
- [ ] **Screener** — sort by market cap descending; top rows must show non-null prices.

All four tabs go through `storage.factory.get_prices_repo` → will silently be reading from Parquet by now. If any tab is empty / errors / hangs, capture the log and STOP. Do not proceed to step 5.

Kill the dashboard when done (Ctrl-C in terminal).

---

## [5] F2d — Drop cloud historical_prices

**Where:** Supabase SQL Editor as `postgres`.

**What:** open [scripts/supabase_free_tier_cleanup.sql](../scripts/supabase_free_tier_cleanup.sql), find **Stanza B**, uncomment the `DROP TABLE` line and the confirmation `SELECT`, then run:

```sql
DROP TABLE IF EXISTS public.historical_prices;

SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size_after_B;
SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;
```

**Recommended snapshot before:** tarball the local Parquet store — that becomes the sole surviving copy:

```bash
# From project root
tar -czf data/prices_backup_$(date +%Y%m%d).tar.gz data/prices/
```

(On Windows, use 7-Zip or PowerShell `Compress-Archive -Path data/prices -DestinationPath data/prices_backup.zip`.)

**Expected result:** DB size drops from ~3208 MB to ~30 MB. `historical_prices` no longer listed in `pg_tables`.

**Rollback:** documented at the bottom of [scripts/supabase_free_tier_cleanup.sql](../scripts/supabase_free_tier_cleanup.sql). Re-create the DDL and reverse-seed from local Parquet.

---

## [6] Post-cleanup verification

Two quick checks — one in Supabase, one in your local terminal.

**In Supabase SQL Editor:**

```sql
-- Should be ~30 MB
SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size;

-- 4 tables (no historical_prices)
SELECT tablename FROM pg_tables WHERE schemaname='public';

-- Still RLS-protected
SELECT tablename, rowsecurity, forcerowsecurity
FROM pg_tables t
JOIN pg_class c ON c.relname = t.tablename
WHERE t.schemaname = 'public';
```

**In local terminal:**

```bash
python scripts/verify_supabase_rls.py
# Expect: SUMMARY: ALL PASS
```

---

## Ongoing operations after the migration

The EOD price refresh must now write to Parquet, not Supabase. The routing lives in `scheduler/job_runner._refresh_historical_prices`; since it already calls `get_prices_repo(...).upsert_rows(...)`, it will pick up the Parquet repo transparently. No code change required.

**One follow-up worth doing later:** rename `scripts/supabase_schema.sql`'s `historical_prices` block into a documented "was hosted in cloud; migrated to local Parquet P3.16 / 2026-07-30" comment. Not urgent — the routing already works — but keeps the schema file honest.

**Env-var kill-switch:** if for any reason you need to force reads back to Supabase (e.g. you re-populated it as a hot standby), set `USE_PARQUET_PRICES=false` in `.env`. The factory checks this on every `get_prices_repo` call — no restart needed if callers pull a fresh repo each time.

---

## What NOT to do

- Don't run Stanza B before smoke tests pass. There is no undo besides the reverse seed.
- Don't run the seed script against production-critical tickers with `--force` while the dashboard is live — the write serialises per file, so a live reader might see a `pd.read_parquet` on a file mid-rewrite. Safe outside dashboard hours.
- Don't delete `data/prices/` after Stanza B has run — that's now the sole copy.
- Don't skip the tarball backup before Stanza B. Discipline.
