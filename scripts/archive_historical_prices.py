"""Archive + prune Supabase `historical_prices` for the retention policy
(perf P2.10). Writes rows older than the cutoff to a per-ticker Parquet
dataset on the local `data/archive/` bind-mounted volume, then deletes
those rows from Supabase.

Choices baked in via the perf redesign plan P2.10 + the retention-
destination question (2026-07-14):
  - 10 y rolling window from today's date (default: date < 2016-01-01
    when run in July 2026). Overridable via --cutoff YYYY-MM-DD.
  - Parquet on local disk. Rehydration is a `pd.read_parquet(path)` or a
    DuckDB `SELECT * FROM 'path/*.parquet'` — no boto3, no cloud creds.

Dry-run by default: prints the row count + resulting size but touches
nothing. Pass --apply to actually write + delete. Requires the current
SUPABASE_DB_URL to be either `postgres` (superuser) or a role with
DELETE privilege on `historical_prices` — `app_backend` from the RLS
migration already has DELETE via the FOR ALL policy.

Usage:
    # Dry-run (default): count + size, no writes
    venv/Scripts/python scripts/archive_historical_prices.py

    # Actually archive + delete rows older than 2016-01-01
    venv/Scripts/python scripts/archive_historical_prices.py --apply

    # Custom cutoff
    venv/Scripts/python scripts/archive_historical_prices.py \\
        --cutoff 2011-01-01 --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# Make the project importable when run as `python scripts/archive_historical_prices.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _default_cutoff() -> str:
    """10 y ago, first-of-January.

    Deterministic and easy to reason about ('everything before Jan 1
    of the year 10 years ago'). Rounding to Jan-1 keeps the archive
    files self-contained per era.
    """
    today = date.today()
    return date(today.year - 10, 1, 1).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", default=_default_cutoff(),
                          help="ISO date; rows with `date` STRICTLY LESS than "
                               "this get archived+deleted. Default: 10y ago, "
                               "Jan 1 (currently %(default)s).")
    parser.add_argument("--apply", action="store_true",
                          help="Actually write archive files + delete rows. "
                               "Without this flag the script is a no-op dry-run.")
    parser.add_argument("--archive-dir", default="data/archive/historical_prices",
                          help="Where to write per-year Parquet files.")
    parser.add_argument("--batch-size", type=int, default=250_000,
                          help="Rows to fetch + write per pass. Keeps memory "
                               "bounded on the 16.9M-row table.")
    args = parser.parse_args()

    import pandas as pd
    from storage import cloud_db

    archive_root = Path(args.archive_dir)

    # ------------- Count + size in dry-run --------------------------
    with cloud_db.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*)                                      AS n_rows,
                   MIN(date)                                     AS min_date,
                   MAX(date)                                     AS max_date,
                   pg_size_pretty(pg_total_relation_size('historical_prices'))
                                                                 AS total_size
            FROM historical_prices
            WHERE date < %s
        """, (args.cutoff,))
        n_rows, min_date, max_date, total_size = cur.fetchone()

    print(f"Cutoff:            {args.cutoff}")
    print(f"Rows to archive:   {n_rows:,}")
    print(f"Date range:        {min_date} → {max_date if max_date else 'N/A'}")
    print(f"Total table size:  {total_size}  (index + heap; freed space "
          "reclaimed only after VACUUM)")
    print(f"Archive dir:       {archive_root}")

    if n_rows == 0:
        print("Nothing to archive at this cutoff — exiting cleanly.")
        return 0

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to actually write + delete.")
        return 0

    # ------------- Archive path ------------------------------------
    archive_root.mkdir(parents=True, exist_ok=True)

    # Fetch in batches, write per-year Parquet. Keeps each output file to
    # a reasonable size (~200-500 MB uncompressed) and lets a rehydration
    # query filter by file rather than scanning everything.
    print()
    print("Archiving to Parquet…")
    total_written = 0
    seen_years: set[int] = set()

    offset = 0
    while True:
        with cloud_db.cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT ticker, market, date, open, high, low, close,
                       adj_close, volume, fetched_at
                FROM historical_prices
                WHERE date < %s
                ORDER BY date, ticker
                LIMIT %s OFFSET %s
            """, (args.cutoff, args.batch_size, offset))
            rows = cur.fetchall()

        if not rows:
            break
        df = pd.DataFrame(rows)
        df["year"] = pd.to_datetime(df["date"]).dt.year
        for year, chunk in df.groupby("year", sort=True):
            path = archive_root / f"historical_prices_{year}.parquet"
            mode = "append" if year in seen_years else "overwrite"
            if mode == "append" and path.exists():
                # Append: read existing, concat, rewrite. Cheap because
                # each year's file is at most ~500 MB and this runs once.
                existing = pd.read_parquet(path)
                chunk = pd.concat([existing, chunk], ignore_index=True)
            chunk.drop(columns=["year"]).to_parquet(
                path, engine="pyarrow", compression="snappy", index=False,
            )
            seen_years.add(year)
        total_written += len(rows)
        print(f"  batch @ offset {offset:>10,}: +{len(rows):,} rows "
              f"(total: {total_written:,} / {n_rows:,})")
        offset += len(rows)

    # ------------- Verify archive integrity ------------------------
    archive_row_count = 0
    for path in sorted(archive_root.glob("historical_prices_*.parquet")):
        n = len(pd.read_parquet(path, columns=["ticker"]))
        archive_row_count += n
        print(f"  {path.name:40s} {n:>10,} rows "
              f"({path.stat().st_size / (1024 * 1024):.1f} MB)")
    if archive_row_count != total_written:
        print(f"FAIL: archive contains {archive_row_count:,} rows but wrote "
              f"{total_written:,}. Aborting BEFORE delete.")
        return 1

    # ------------- Delete from Supabase ----------------------------
    print()
    print(f"Archive integrity OK. Deleting {total_written:,} rows from Supabase…")
    with cloud_db.cursor() as cur:
        cur.execute("DELETE FROM historical_prices WHERE date < %s",
                     (args.cutoff,))
        deleted = cur.rowcount
    print(f"Deleted: {deleted:,} rows")

    if deleted != total_written:
        print(f"WARN: deleted {deleted:,} but archived {total_written:,}. "
              "Likely a concurrent insert happened during the archive pass. "
              "Re-run for a full sweep.")

    # Report post-delete size. Note: `pg_total_relation_size` doesn't
    # shrink until VACUUM (heap holds the tombstones). Autovacuum will
    # reclaim over the next few hours; for immediate reclaim run
    # `VACUUM FULL historical_prices` in the SQL Editor (requires
    # exclusive lock — do off-hours).
    with cloud_db.cursor() as cur:
        cur.execute("SELECT pg_size_pretty(pg_total_relation_size("
                    "'historical_prices'))")
        new_size = cur.fetchone()[0]
    print(f"Table size now:    {new_size}  (heap tombstones — run "
          "VACUUM FULL off-hours to reclaim)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
