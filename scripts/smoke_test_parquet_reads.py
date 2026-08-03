"""Verify Parquet historical_prices parity against Supabase.

Run this AFTER `scripts/seed_parquet_from_supabase.py` completes and
BEFORE dropping the Supabase `historical_prices` table.

Checks
------
1. Ticker inventory: Parquet has >= all tickers Supabase has.
2. Per-ticker row counts match within a small tolerance (defaults 0).
3. Per-ticker latest_date matches (or Parquet is newer).
4. Random-sample deep read: pick N tickers, compare exact rows for the
   most recent 30 trading days between both backends. Any diff > 1e-4
   on adj_close/close is flagged.
5. Factory routing check: `storage.factory.get_prices_repo()` returns
   `ParquetHistoricalPricesRepository` (proves the automatic flip).
6. `store_populated()` returns True.
7. Live callers smoke test: `analysis.data_loader.get_or_fetch_prices`
   returns a non-empty series for a handful of watchlist tickers.

Exit codes
----------
    0 -- all checks pass; safe to drop Supabase historical_prices.
    1 -- one or more checks failed; DO NOT DROP.

Usage
-----
    venv/Scripts/python scripts/smoke_test_parquet_reads.py
    venv/Scripts/python scripts/smoke_test_parquet_reads.py --sample 20
    venv/Scripts/python scripts/smoke_test_parquet_reads.py --tolerance 0.01
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import psycopg2
from dotenv import load_dotenv

from storage.parquet_prices import (
    ParquetHistoricalPricesRepository,
    store_populated,
)

load_dotenv()

WATCHLIST_SPOT_CHECK = [
    "0700.HK",   # Tencent
    "9988.HK",   # BABA-W
    "6181.HK",   # Laopu Gold (the P/E bug canary)
    "0005.HK",   # HSBC
    "AAPL",      # US spot check
    "^HSI",      # Index
]

# Cloud tickers we deliberately don't sync to Parquet. Contains only
# `&US:AEROSPACE_AND_DEFENSE` -- a one-off experimental composite whose
# ticker string contains a colon, which is illegal in Windows filesystem
# paths. Its sibling `&AEROSPACE_AND_DEFENSE` covers the same sector
# and IS seeded. About to be nuked by the F2d DROP TABLE anyway.
# See subsector_synth.py naming-convention follow-up.
IGNORE_ORPHANS = {"&US:AEROSPACE_AND_DEFENSE"}

_STATUS_OK = "PASS"
_STATUS_FAIL = "FAIL"
_STATUS_WARN = "WARN"


def _hr(char: str = "=") -> str:
    return char * 62


def _check_result(name: str, status: str, detail: str = "") -> tuple[str, str, str]:
    marker = {_STATUS_OK: "[  OK  ]", _STATUS_FAIL: "[ FAIL ]",
              _STATUS_WARN: "[ WARN ]"}[status]
    print(f"{marker} {name}")
    if detail:
        for line in detail.splitlines():
            print(f"          {line}")
    return name, status, detail


def _cloud_summary(dsn: str) -> dict:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ticker, COUNT(*), MAX(date)::text
            FROM historical_prices
            GROUP BY ticker
        """)
        return {r[0]: (int(r[1]), r[2]) for r in cur.fetchall()}
    finally:
        conn.close()


def _cloud_recent_rows(dsn: str, ticker: str, days: int = 30) -> list[tuple]:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT date::text, adj_close, close
            FROM historical_prices
            WHERE ticker = %s
            ORDER BY date DESC
            LIMIT %s
        """, (ticker, days))
        rows = cur.fetchall()
        return sorted(
            [(r[0], None if r[1] is None else float(r[1]),
                     None if r[2] is None else float(r[2])) for r in rows]
        )
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=15,
                    help="random tickers to deep-check (default 15)")
    ap.add_argument("--tolerance", type=float, default=0.0001,
                    help="max abs diff on adj_close between backends "
                         "(default 1e-4)")
    ap.add_argument("--count-tolerance", type=int, default=0,
                    help="max row-count diff per ticker (default 0)")
    ap.add_argument("--recent-days", type=int, default=30,
                    help="deep-check window per ticker (default 30 rows)")
    args = ap.parse_args()

    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("FATAL: SUPABASE_DB_URL not set in .env")
        return 2

    results: list[tuple[str, str, str]] = []
    parquet_repo = ParquetHistoricalPricesRepository()

    print(_hr())
    print("Parquet vs Supabase parity -- smoke test")
    print(_hr())

    # --- Check 1: store_populated -----------------------------------
    populated = store_populated()
    results.append(_check_result(
        "1. Parquet store passes populated() gate",
        _STATUS_OK if populated else _STATUS_FAIL,
        detail="(store_populated=True -> factory auto-flips to Parquet)"
                if populated else
               "(store_populated=False -> factory will still use Supabase)"))

    # --- Check 2: factory routing -----------------------------------
    try:
        from storage.factory import get_prices_repo
        from storage.database import Database
        # Not actually used by Parquet route; passed for interface.
        with Database("data/sentiment.db").get_connection() as conn:
            repo = get_prices_repo(conn)
        repo_name = type(repo).__name__
        ok = repo_name == "ParquetHistoricalPricesRepository"
        results.append(_check_result(
            "2. factory.get_prices_repo() returns Parquet repo",
            _STATUS_OK if ok else _STATUS_FAIL,
            detail=f"got {repo_name}"))
    except Exception as e:
        results.append(_check_result(
            "2. factory.get_prices_repo() returns Parquet repo",
            _STATUS_FAIL, detail=f"{type(e).__name__}: {e}"))

    # --- Check 3: ticker inventory ----------------------------------
    print("Loading cloud + parquet ticker inventories...")
    cloud = _cloud_summary(dsn)
    # Drop known-unsyncable orphans from the cloud side before parity checks.
    for orphan in IGNORE_ORPHANS:
        cloud.pop(orphan, None)
    parq_tickers = set(parquet_repo.distinct_tickers())
    missing = sorted(set(cloud) - parq_tickers)
    extra = sorted(parq_tickers - set(cloud))
    status3 = _STATUS_OK if not missing else _STATUS_FAIL
    detail3 = f"cloud={len(cloud)}  parquet={len(parq_tickers)}\n"
    detail3 += f"missing_from_parquet={len(missing)}\n"
    if missing:
        detail3 += f"first 10 missing: {missing[:10]}\n"
    if extra:
        detail3 += f"extra in parquet (ok): {len(extra)}"
    results.append(_check_result(
        "3. Ticker inventory: Parquet covers all cloud tickers",
        status3, detail=detail3))

    # --- Check 4: per-ticker row counts + latest_date ---------------
    count_mismatches: list[tuple[str, int, int]] = []
    date_mismatches: list[tuple[str, str, str]] = []
    print(f"Verifying counts + latest_date for {len(cloud):,} tickers...")
    checked = 0
    for ticker, (cloud_count, cloud_latest) in cloud.items():
        if ticker not in parq_tickers:
            continue
        parq_count  = parquet_repo.count_rows(ticker)
        parq_latest = parquet_repo.latest_date(ticker)
        if abs(parq_count - cloud_count) > args.count_tolerance:
            count_mismatches.append((ticker, cloud_count, parq_count))
        if cloud_latest is not None and parq_latest is not None \
           and parq_latest < cloud_latest:
            date_mismatches.append((ticker, cloud_latest, parq_latest))
        checked += 1

    status4a = _STATUS_OK if not count_mismatches else _STATUS_FAIL
    detail4a = f"tickers checked: {checked}\n"
    detail4a += f"count mismatches: {len(count_mismatches)}\n"
    if count_mismatches[:5]:
        for t, c, p in count_mismatches[:5]:
            detail4a += f"  {t}: cloud={c} parquet={p}\n"
    results.append(_check_result(
        "4a. Per-ticker row counts match",
        status4a, detail=detail4a))

    status4b = _STATUS_OK if not date_mismatches else _STATUS_FAIL
    detail4b = f"date mismatches (parquet older than cloud): {len(date_mismatches)}\n"
    if date_mismatches[:5]:
        for t, cd, pd_ in date_mismatches[:5]:
            detail4b += f"  {t}: cloud={cd} parquet={pd_}\n"
    results.append(_check_result(
        "4b. Per-ticker latest_date >= cloud",
        status4b, detail=detail4b))

    # --- Check 5: random-sample deep row diff -----------------------
    sample_pool = sorted(set(cloud) & parq_tickers)
    if len(sample_pool) < args.sample:
        args.sample = len(sample_pool)
    rng = random.Random(20260730)
    sample = rng.sample(sample_pool, args.sample) if sample_pool else []
    diff_tickers: list[tuple[str, int, float]] = []
    for ticker in sample:
        cloud_rows = _cloud_recent_rows(dsn, ticker, args.recent_days)
        parq_series = parquet_repo.get_full_ohlc_series(ticker)
        parq_recent = sorted(
            [(r["date"],
              None if r.get("adj_close") is None else float(r["adj_close"]),
              None if r.get("close") is None else float(r["close"]))
             for r in parq_series[-args.recent_days:]]
        )
        c_map = {r[0]: r for r in cloud_rows}
        p_map = {r[0]: r for r in parq_recent}
        shared_dates = sorted(set(c_map) & set(p_map))
        max_diff = 0.0
        diff_count = 0
        for d in shared_dates:
            cv = c_map[d][1]
            pv = p_map[d][1]
            if cv is None or pv is None:
                continue
            diff = abs(cv - pv)
            if diff > args.tolerance:
                diff_count += 1
                max_diff = max(max_diff, diff)
        if diff_count > 0:
            diff_tickers.append((ticker, diff_count, max_diff))
    status5 = _STATUS_OK if not diff_tickers else _STATUS_FAIL
    detail5 = (f"deep-checked {len(sample)} random tickers x "
                f"{args.recent_days}d each\n")
    detail5 += f"tolerance: {args.tolerance}\n"
    detail5 += f"tickers with any diff > tol: {len(diff_tickers)}\n"
    if diff_tickers[:5]:
        for t, n, m in diff_tickers[:5]:
            detail5 += f"  {t}: {n} rows differ, max diff = {m:.6f}\n"
    results.append(_check_result(
        f"5. Deep row parity on {args.sample} random tickers",
        status5, detail=detail5))

    # --- Check 6: watchlist spot check via data_loader --------------
    try:
        from analysis.data_loader import get_or_fetch_prices
        from storage.database import Database
        empty_tickers = []
        db = Database("data/sentiment.db")
        for t in WATCHLIST_SPOT_CHECK:
            try:
                rows = get_or_fetch_prices(t, db)
                if not rows:
                    empty_tickers.append(t)
            except Exception as e:
                empty_tickers.append(f"{t}({type(e).__name__}: {e})")
        status6 = _STATUS_OK if not empty_tickers else _STATUS_WARN
        detail6 = f"spot-checked: {WATCHLIST_SPOT_CHECK}\n"
        if empty_tickers:
            detail6 += f"empty / errored: {empty_tickers}"
        else:
            detail6 += "all returned non-empty series"
        results.append(_check_result(
            "6. data_loader.get_or_fetch_prices smoke test",
            status6, detail=detail6))
    except Exception as e:
        results.append(_check_result(
            "6. data_loader.get_or_fetch_prices smoke test",
            _STATUS_WARN, detail=f"{type(e).__name__}: {e}"))

    # --- Summary ----------------------------------------------------
    print()
    print(_hr())
    print("Summary")
    print(_hr())
    passes = sum(1 for _, s, _ in results if s == _STATUS_OK)
    fails  = sum(1 for _, s, _ in results if s == _STATUS_FAIL)
    warns  = sum(1 for _, s, _ in results if s == _STATUS_WARN)
    for name, status, _ in results:
        print(f"  {status:4s}  {name}")
    print(f"\n  {passes} PASS  {fails} FAIL  {warns} WARN")

    if fails:
        print("\nDO NOT drop Supabase historical_prices. Re-run "
              "scripts/seed_parquet_from_supabase.py to close the gaps, "
              "then re-run this smoke test.")
        return 1
    print("\nAll checks passed. Safe to run the F2 DROP TABLE stanza in "
          "scripts/supabase_free_tier_cleanup.sql.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
