"""Bulk-seed the local Parquet historical_prices store from Supabase.

This is the one-shot data move required by the P3.16 migration that was
abandoned mid-way (only 0700.HK was seeded originally). After this
script completes for all ~7 k tickers, `storage.factory.get_prices_repo`
will automatically flip to `ParquetHistoricalPricesRepository` on the
next call because `storage.parquet_prices.store_populated()` gates on
`>= 100 ticker dirs`.

Once verified via `scripts/smoke_test_parquet_reads.py`, the Supabase
`historical_prices` table can be dropped to reclaim ~3.9 GB — see
`docs/supabase_free_tier_cleanup.md`.

Behaviour
---------
- Resumable: a per-ticker checkpoint at `data/.parquet_seed_checkpoint.json`
  is updated after each ticker completes. Re-running is cheap; already-
  complete tickers are skipped unless their Parquet `latest_date` is older
  than Supabase's `latest_date` (in which case the delta is copied).
- Parallel: 6 concurrent Supabase readers by default. The threaded pool
  has a max of 10 conns; leaving headroom for other callers.
- Deterministic: reads rows in `ORDER BY date` per ticker; writes go
  through `ParquetHistoricalPricesRepository.upsert_rows` which merges
  per-year files idempotently. Re-running with no new data is a no-op.
- Progress: prints a `[N/M]` line per ticker with rows-copied + elapsed.
  No `tqdm` dependency.

Usage
-----
    # Full seed (all tickers)
    venv/Scripts/python scripts/seed_parquet_from_supabase.py

    # Test on a small subset first
    venv/Scripts/python scripts/seed_parquet_from_supabase.py --limit 20

    # Force a re-seed of specific tickers (ignores checkpoint)
    venv/Scripts/python scripts/seed_parquet_from_supabase.py --force 0700.HK 6181.HK

    # Change concurrency (default 6)
    venv/Scripts/python scripts/seed_parquet_from_supabase.py --workers 4

Zero new dependencies. Uses psycopg2 (already installed), pandas +
pyarrow (already installed), stdlib argparse/json/concurrent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

# Ensure project root is importable when run as `python scripts/...`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import psycopg2
from dotenv import load_dotenv

from storage.parquet_prices import ParquetHistoricalPricesRepository

load_dotenv()

CHECKPOINT_PATH = _ROOT / "data" / ".parquet_seed_checkpoint.json"
DEFAULT_WORKERS = 6
FETCH_COLUMNS = ("date", "open", "high", "low", "close", "adj_close", "volume")

_print_lock = Lock()
_ckpt_lock = Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _load_checkpoint() -> dict:
    if not CHECKPOINT_PATH.exists():
        return {}
    try:
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_checkpoint(ckpt: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ckpt, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(CHECKPOINT_PATH)


def _fetch_cloud_ticker_summary(dsn: str) -> dict:
    """Returns {ticker: (row_count, latest_date_iso)}. One query."""
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ticker, COUNT(*), MAX(date)::text
            FROM historical_prices
            GROUP BY ticker
            ORDER BY ticker
        """)
        return {row[0]: (int(row[1]), row[2]) for row in cur.fetchall()}
    finally:
        conn.close()


def _fetch_ticker_rows(dsn: str, ticker: str,
                       start_after: str | None) -> list[dict]:
    """Stream one ticker's rows in chronological order. If `start_after`
    is set (ISO date), only newer rows are pulled — the incremental
    catch-up path."""
    conn = psycopg2.connect(dsn)
    try:
        # Sanitise ticker for use as a psycopg2 named-cursor identifier —
        # Postgres identifiers only allow [A-Za-z0-9_], so aggressively
        # replace everything else (covers `.`, `^`, `@`, `&`, `:`, etc.).
        import re
        safe = re.sub(r"[^A-Za-z0-9_]", "_", ticker)
        cur = conn.cursor(name=f"seed_{safe}")
        cur.itersize = 5000
        if start_after:
            cur.execute("""
                SELECT date::text, open, high, low, close, adj_close, volume
                FROM historical_prices
                WHERE ticker = %s AND date > %s
                ORDER BY date
            """, (ticker, start_after))
        else:
            cur.execute("""
                SELECT date::text, open, high, low, close, adj_close, volume
                FROM historical_prices
                WHERE ticker = %s
                ORDER BY date
            """, (ticker,))
        rows = []
        for r in cur:
            rows.append({
                "date":      r[0],
                "open":      None if r[1] is None else float(r[1]),
                "high":      None if r[2] is None else float(r[2]),
                "low":       None if r[3] is None else float(r[3]),
                "close":     None if r[4] is None else float(r[4]),
                "adj_close": None if r[5] is None else float(r[5]),
                "volume":    None if r[6] is None else int(r[6]),
            })
        return rows
    finally:
        conn.close()


def _process_ticker(dsn: str, ticker: str, cloud_count: int,
                    cloud_latest: str | None,
                    parquet_repo: ParquetHistoricalPricesRepository,
                    force: bool) -> tuple[str, int, float, str]:
    """Returns (ticker, rows_written, seconds, status)."""
    t0 = time.time()
    local_latest = parquet_repo.latest_date(ticker) if not force else None
    local_count  = parquet_repo.count_rows(ticker) if not force else 0

    # Fast path: local already covers cloud
    if (not force
        and local_latest is not None
        and cloud_latest is not None
        and local_latest >= cloud_latest
        and local_count >= cloud_count):
        return ticker, 0, time.time() - t0, "skip-uptodate"

    start_after = local_latest if (not force and local_latest) else None
    rows = _fetch_ticker_rows(dsn, ticker, start_after)
    written = parquet_repo.upsert_rows(ticker, rows) if rows else 0
    return ticker, written, time.time() - t0, ("incremental" if start_after else "full")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"concurrent Supabase readers (default {DEFAULT_WORKERS}, max 10)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only seed the first N tickers (for testing)")
    ap.add_argument("--force", nargs="*", default=[],
                    help="specific tickers to re-seed from scratch")
    ap.add_argument("--only", nargs="*", default=[],
                    help="restrict to these tickers only (skip everything else)")
    args = ap.parse_args()

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        _log("FATAL: SUPABASE_DB_URL not set in .env")
        return 2

    if args.workers > 10:
        _log(f"WARNING: --workers={args.workers} > pool cap 10; clamping to 8")
        args.workers = 8

    _log("=" * 60)
    _log(f"Parquet seed from Supabase — {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    _log("=" * 60)
    _log("Step 1/3: inventory Supabase tickers...")

    t0 = time.time()
    cloud_summary = _fetch_cloud_ticker_summary(dsn)
    total_rows_cloud = sum(c for c, _ in cloud_summary.values())
    _log(f"  {len(cloud_summary):,} tickers / {total_rows_cloud:,} rows "
         f"in cloud (took {time.time()-t0:.1f}s)")

    tickers = sorted(cloud_summary.keys())
    if args.only:
        wanted = set(args.only)
        tickers = [t for t in tickers if t in wanted]
        _log(f"  --only filter: {len(tickers)} tickers")
    if args.limit:
        tickers = tickers[:args.limit]
        _log(f"  --limit {args.limit}: seeding first {len(tickers)} tickers")

    force_set = set(args.force)
    parquet_repo = ParquetHistoricalPricesRepository()
    ckpt = _load_checkpoint()

    _log(f"Step 2/3: seed {len(tickers)} tickers with {args.workers} workers...")
    total_written = 0
    total_skipped = 0
    total_incremental = 0
    total_full = 0
    total_errors = 0
    started = time.time()
    completed = 0

    def _handle_result(ticker, written, secs, status):
        nonlocal total_written, total_skipped, total_incremental, total_full, completed
        completed += 1
        elapsed = time.time() - started
        rate = completed / elapsed if elapsed > 0 else 0
        eta = (len(tickers) - completed) / rate if rate > 0 else 0
        total_written += written
        if status == "skip-uptodate":
            total_skipped += 1
        elif status == "incremental":
            total_incremental += 1
        elif status == "full":
            total_full += 1
        _log(f"  [{completed:5d}/{len(tickers)}] {ticker:15s} "
             f"{status:14s} +{written:6d} rows  {secs:5.1f}s  "
             f"(rate {rate:5.1f}/s  ETA {eta/60:5.1f}m)")
        with _ckpt_lock:
            ckpt[ticker] = {
                "status": status,
                "rows_written": written,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if completed % 50 == 0:
                _save_checkpoint(ckpt)

    with ThreadPoolExecutor(max_workers=args.workers,
                            thread_name_prefix="parquet-seed") as pool:
        futures = {
            pool.submit(_process_ticker, dsn, t,
                        cloud_summary[t][0], cloud_summary[t][1],
                        parquet_repo, t in force_set): t
            for t in tickers
        }
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                result = fut.result()
                _handle_result(*result)
            except Exception as e:
                total_errors += 1
                completed += 1
                _log(f"  [{completed:5d}/{len(tickers)}] {t:15s} "
                     f"ERROR          {type(e).__name__}: {e}")

    _save_checkpoint(ckpt)

    _log("")
    _log("Step 3/3: verification...")
    parquet_tickers = parquet_repo.distinct_tickers()
    _log(f"  Parquet store now: {len(parquet_tickers):,} tickers")
    _log(f"  Cloud reference:   {len(cloud_summary):,} tickers")
    missing = [t for t in tickers if t not in set(parquet_tickers)]
    if missing:
        _log(f"  MISSING from Parquet: {len(missing)} tickers "
             f"(first 10: {missing[:10]})")
    else:
        _log("  All targeted tickers present in Parquet")

    total_elapsed = time.time() - started
    _log("")
    _log("=" * 60)
    _log("Summary")
    _log("=" * 60)
    _log(f"  Total time:         {total_elapsed/60:.1f} min")
    _log(f"  Skipped (uptodate): {total_skipped:>6}")
    _log(f"  Incremental:        {total_incremental:>6}")
    _log(f"  Full seed:          {total_full:>6}")
    _log(f"  Errors:             {total_errors:>6}")
    _log(f"  Rows written:       {total_written:>12,}")
    _log(f"  Checkpoint:         {CHECKPOINT_PATH}")
    _log("")
    if total_errors:
        _log(f"NOTE: {total_errors} tickers errored. Re-run to retry "
             "(idempotent — completed tickers are skipped).")
        return 1
    _log("Next step: run scripts/smoke_test_parquet_reads.py to verify parity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
