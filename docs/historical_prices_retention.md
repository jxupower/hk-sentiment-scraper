# `historical_prices` retention policy (perf P2.10)

**Policy**: keep the last 10 years of daily bars on Supabase; archive
older rows to per-year Parquet files on `data/archive/historical_prices/`
(the same bind mount as `data/sentiment.db`).

**Why**: `historical_prices` has grown to 16.9 M rows (~5-6 GB with
indexes) — 10× the Supabase free-tier 500 MB ceiling. Growth is ~2.5 M
rows/year. The 10 y window covers every default Backtest UI preset
(1Y/3Y/5Y) plus one comfortable 10Y max, matches the last full business
cycle (COVID crash → recovery, 2018 selloff, current era), and reclaims
~1.5 GB immediately.

**Reversibility**: 100%. Archived rows live as Parquet on disk;
rehydration is a one-liner (see below). Data itself is regenerable
from yfinance / akshare if the local archive is ever lost.

## Running it

Dry-run first (no writes, no deletes):

```bash
venv/Scripts/python scripts/archive_historical_prices.py
```

Output shape:

```
Cutoff:            2016-01-01
Rows to archive:   4,821,336
Date range:        2010-01-04 → 2015-12-31
Total table size:  5620 MB  (index + heap; freed space reclaimed only after VACUUM)
Archive dir:       data/archive/historical_prices

Dry-run only. Re-run with --apply to actually write + delete.
```

If the number of rows and the date range look right:

```bash
venv/Scripts/python scripts/archive_historical_prices.py --apply
```

The script:
1. Fetches the pre-cutoff rows from Supabase in 250 k-row batches
2. Writes them to `data/archive/historical_prices/historical_prices_<YEAR>.parquet`
   (Snappy compression, pyarrow engine)
3. Verifies the archive row count matches what it wrote
4. `DELETE FROM historical_prices WHERE date < <cutoff>` on Supabase

Only step 4 happens if step 3 verified clean. Concurrent inserts during
the archive pass are flagged with a warning (re-run picks them up).

## After the delete: reclaim the space

Postgres holds row tombstones until `VACUUM` reclaims them. Autovacuum
runs opportunistically over the next few hours; for immediate reclaim:

Supabase SQL Editor as `postgres`:

```sql
VACUUM FULL historical_prices;   -- takes ACCESS EXCLUSIVE lock; do off-hours
```

`VACUUM FULL` is blocking — the dashboard and scheduler stall for the
duration (~1-3 min for this table). Run when nobody's clicking around,
or accept the brief unavailability. Alternatively `pg_repack` (an
extension) does the same rewrite without the lock, but Supabase doesn't
have it installed by default.

## Rehydrating archived data

For a one-off backtest that needs pre-2016 data, either:

**Option A — read Parquet directly with pandas or DuckDB** (no rewrite
of Supabase). This is the intended path for occasional deep-history use.

```python
import pandas as pd
# All years, one call
df = pd.read_parquet("data/archive/historical_prices/")
# Just 2010-2012
years = [2010, 2011, 2012]
df = pd.concat([
    pd.read_parquet(f"data/archive/historical_prices/historical_prices_{y}.parquet")
    for y in years
])
```

**Option B — bulk-restore into Supabase** (only if you decide to widen
the retention window permanently):

```sql
-- 1. Move the parquet files to a location Supabase's Storage API can
--    read, or use `psql \copy` from the VM. `psycopg2.extras.execute_values`
--    from a local Python script also works (see scripts/migrate_to_supabase.py
--    for the pattern) — takes ~30-60 s per million rows.
```

## Recurring retention (once we're ready)

The plan intends this to be a **quarterly** scheduled job — not
runtime-continuous. Currently unscheduled: pin the reminder to a
calendar event or add it as a monthly APScheduler job once we've done
the first archive pass and confirmed the runbook is smooth.

Suggested cron (add to `scheduler/job_runner.py:start()` when ready):

```python
self._scheduler.add_job(
    func=lambda: subprocess.run(
        [sys.executable, "scripts/archive_historical_prices.py", "--apply"],
        check=True),
    trigger="cron", day="1", month="1,4,7,10", hour="6",
    id="historical_prices_archive_quarterly", max_instances=1,
)
```

## What this does NOT do

- **Doesn't touch `fundamentals_snapshots`.** That table is much smaller
  (~25 k rows) and grows slowly (the daily yfinance cron is disabled per
  CLAUDE.md's cost-control note).
- **Doesn't compress or reformat rows still in Supabase.** Postgres
  storage of NUMERIC + BIGINT is what it is; column-store migration is
  P3.16, separate plan.
- **Doesn't remove indexes.** `idx_hp_ticker_date` stays. `idx_hp_
  market_ticker_date` was already dropped in P1.6.
