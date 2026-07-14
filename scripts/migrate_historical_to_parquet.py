"""One-shot migration: copy Supabase `historical_prices` into a local
Hive-partitioned Parquet store at `data/prices/<TICKER>/year=<YEAR>.parquet`
(perf P3.16).

Idempotent — re-runs skip tickers whose local Parquet store already
covers the full Supabase date range (`min(supabase.date) ==
min(parquet.date)` AND `max(supabase.date) == max(parquet.date)`).
Passing --force ignores the coverage check and rewrites every ticker.

Wall-clock: ~15-30 min for the ~16.9 M rows currently on Supabase,
dominated by Supabase read latency, not local write speed. Migration
uses 8 parallel workers × per-ticker fetches; total network round-trips
= ~7 k (one SELECT per ticker + one COUNT for the coverage check).

The Supabase `historical_prices` table is NOT dropped by this migration.
That's a separate manual `DROP TABLE` step after operator confidence
that the local Parquet store is complete and the app routes cleanly.

Usage:
    # Dry-run (default): reports ticker count + estimated wall-clock
    venv/Scripts/python scripts/migrate_historical_to_parquet.py

    # Actually migrate
    venv/Scripts/python scripts/migrate_historical_to_parquet.py --apply

    # Rewrite even tickers that look up-to-date locally
    venv/Scripts/python scripts/migrate_historical_to_parquet.py --apply --force

    # Migrate a specific ticker set (comma-separated) — useful for spot-check
    venv/Scripts/python scripts/migrate_historical_to_parquet.py \\
        --apply --tickers 0700.HK,^HSI,AAPL
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Make the project importable when run as `python scripts/migrate_historical_to_parquet.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fetch_supabase_tickers() -> list[str]:
    from storage import cloud_db
    with cloud_db.cursor() as cur:
        cur.execute("SELECT DISTINCT ticker FROM historical_prices "
                    "ORDER BY ticker")
        return [r[0] for r in cur.fetchall()]


def _supabase_range(ticker: str) -> tuple[int, str, str]:
    """(row_count, min_date, max_date) for one ticker."""
    from storage import cloud_db
    with cloud_db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*), MIN(date), MAX(date)
            FROM historical_prices
            WHERE ticker = %s
        """, (ticker,))
        n, mn, mx = cur.fetchone()
    return int(n or 0), (str(mn) if mn else ""), (str(mx) if mx else "")


def _fetch_ticker_rows(ticker: str) -> list[dict]:
    from storage import cloud_db
    with cloud_db.cursor(dict_rows=True) as cur:
        cur.execute("""
            SELECT ticker, date, open, high, low, close, adj_close, volume
            FROM historical_prices
            WHERE ticker = %s
            ORDER BY date
        """, (ticker,))
        rows = cur.fetchall()
    # Map to the shape ParquetHistoricalPricesRepository.upsert_rows expects.
    return [{
        "date": str(r["date"]),
        "open": float(r["open"]) if r["open"] is not None else None,
        "high": float(r["high"]) if r["high"] is not None else None,
        "low": float(r["low"]) if r["low"] is not None else None,
        "close": float(r["close"]) if r["close"] is not None else None,
        "adj_close": float(r["adj_close"]) if r["adj_close"] is not None else None,
        "volume": int(r["volume"]) if r["volume"] is not None else None,
    } for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                          help="Actually write files. Without this, dry-run only.")
    parser.add_argument("--force", action="store_true",
                          help="Ignore the coverage check; rewrite every ticker.")
    parser.add_argument("--tickers", default=None,
                          help="Comma-separated ticker list. Overrides the "
                               "'all tickers on Supabase' default.")
    parser.add_argument("--workers", type=int, default=8,
                          help="Parallel Supabase fetch workers.")
    args = parser.parse_args()

    # ---- Discover tickers ----------------------------------------
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        print("Fetching ticker list from Supabase...")
        tickers = _fetch_supabase_tickers()
    print(f"Supabase distinct tickers: {len(tickers):,}")

    if not tickers:
        print("Nothing to migrate — exiting.")
        return 0

    # ---- Coverage check (skip up-to-date tickers) ----------------
    from storage.parquet_prices import (
        ParquetHistoricalPricesRepository, _ROOT,
    )
    repo = ParquetHistoricalPricesRepository()

    skipped = 0
    to_migrate: list[str] = []
    print(f"Coverage check against local store at {_ROOT}...")
    if args.force:
        to_migrate = list(tickers)
    else:
        # Cheap probe: only checks min/max of the local files. Full
        # row-count equality is too expensive across 7k tickers; the
        # date-range check catches ~99% of real coverage.
        for i, ticker in enumerate(tickers, start=1):
            local_min = repo.earliest_date(ticker)
            local_max = repo.latest_date(ticker)
            if not local_max:
                to_migrate.append(ticker)
                continue
            _n, sb_min, sb_max = _supabase_range(ticker)
            if local_min == sb_min and local_max == sb_max:
                skipped += 1
            else:
                to_migrate.append(ticker)
            if i % 500 == 0:
                print(f"  coverage-check progress: {i}/{len(tickers)} "
                      f"({skipped} skip, {len(to_migrate)} to migrate)")
        print(f"  {skipped} tickers already up-to-date, "
              f"{len(to_migrate)} need migration")

    if not to_migrate:
        print("All tickers up-to-date. Nothing to write.")
        return 0

    # ---- Dry-run summary -----------------------------------------
    if not args.apply:
        print()
        print(f"DRY-RUN: would migrate {len(to_migrate):,} tickers.")
        print(f"  Estimated wall-clock: {len(to_migrate) * 0.3:.0f}-"
              f"{len(to_migrate) * 0.6:.0f}s "
              f"(with {args.workers} workers × ~200-400 ms per ticker)")
        print(f"  Estimated on-disk footprint: "
              f"{len(to_migrate) * 30 / 1024:.1f} MB "
              f"(~30 KB per ticker × 10 years).")
        print()
        print("Re-run with --apply to actually write.")
        return 0

    # ---- Apply --------------------------------------------------
    print()
    print(f"Migrating {len(to_migrate):,} tickers with {args.workers} workers…")
    t_start = time.perf_counter()
    completed = 0
    failed: list[tuple[str, str]] = []
    rows_written = 0

    def _work(ticker: str):
        try:
            rows = _fetch_ticker_rows(ticker)
            if not rows:
                return ticker, 0, None
            n = repo.upsert_rows(ticker, rows)
            return ticker, n, None
        except Exception as e:
            return ticker, 0, str(e)

    with ThreadPoolExecutor(max_workers=args.workers,
                              thread_name_prefix="parquet-migrate") as pool:
        futures = [pool.submit(_work, t) for t in to_migrate]
        for fut in as_completed(futures):
            ticker, n, err = fut.result()
            completed += 1
            if err:
                failed.append((ticker, err))
            else:
                rows_written += n
            if completed % 100 == 0 or completed == len(to_migrate):
                elapsed = time.perf_counter() - t_start
                rate = completed / elapsed if elapsed else 0
                eta = (len(to_migrate) - completed) / rate if rate else 0
                print(f"  [{completed:>5}/{len(to_migrate)}] "
                      f"rows={rows_written:>10,}  "
                      f"failed={len(failed):>3}  "
                      f"{elapsed:>6.0f}s elapsed, ~{eta:.0f}s ETA")

    elapsed = time.perf_counter() - t_start
    print()
    print(f"Done in {elapsed:.0f}s ({elapsed/60:.1f} min).")
    print(f"  tickers migrated: {completed - len(failed):,}")
    print(f"  rows written:     {rows_written:,}")
    print(f"  failed tickers:   {len(failed)}")
    if failed:
        print("First 10 failures:")
        for ticker, err in failed[:10]:
            print(f"    {ticker}: {err[:100]}")
    print()
    print("Post-migration next steps:")
    print("  1. Verify the local store: python -c \"from storage."
          "parquet_prices import store_populated; print(store_populated())\"")
    print("     -> should print True")
    print("  2. Bench chart query: python -c \"import time; from storage."
          "factory import get_prices_repo; from storage.database import "
          "Database; t=time.perf_counter(); r=get_prices_repo(Database("
          "'data/sentiment.db')).get_full_series('0700.HK'); print(f'{len(r)}"
          " rows in {(time.perf_counter()-t)*1000:.1f} ms')\"")
    print("     -> should be ≤50 ms warm (vs ~300-500 ms via Supabase)")
    print("  3. Restart the app; storage.factory routes automatically.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
