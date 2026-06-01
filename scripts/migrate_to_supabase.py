"""One-shot migration: copy historical_prices + fundamentals_snapshots from
local SQLite into Supabase Postgres.

What it does:
  1. Reads from data/sentiment.db
  2. Bulk-inserts into Supabase via psycopg2 batched executemany
     (~10K rows/batch, idempotent via ON CONFLICT DO NOTHING)
  3. Prints row counts before + after, sanity-checks 5 random tickers

What it does NOT do:
  - Fetch new prices/fundamentals from yfinance/akshare — see
    resume_historical_seed.py for that
  - Touch articles, sentiment, signals, securities, research_notes, backtest_*
    (those stay in local SQLite forever)

Usage:
    python scripts/migrate_to_supabase.py [--dry-run] [--limit-tickers 5]
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Make the project importable when run as `python scripts/migrate_to_supabase.py`
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from storage.cloud_db import cursor, available

BATCH_SIZE = 5000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                     help="Print counts; don't actually insert.")
    ap.add_argument("--limit-tickers", type=int, default=0,
                     help="For testing: only migrate N tickers (0 = all).")
    args = ap.parse_args()

    if not settings.cloud_db_configured():
        print("ERROR: Set USE_CLOUD_DB=true and SUPABASE_DB_URL in .env first.")
        return 1
    if not available():
        print("ERROR: Cloud DB ping failed. Check connectivity.")
        return 1

    sqlite_path = settings.DB_PATH
    print(f"Source:      sqlite {sqlite_path}")
    print(f"Destination: Supabase {settings.SUPABASE_DB_URL.split('@')[1].split('/')[0]}")
    print()

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    # ---- Counts before ----
    src_prices_n = src.execute("SELECT COUNT(*) FROM historical_prices").fetchone()[0]
    src_funds_n  = src.execute("SELECT COUNT(*) FROM fundamentals_snapshots").fetchone()[0]
    src_tickers  = src.execute("SELECT COUNT(DISTINCT ticker) FROM historical_prices").fetchone()[0]
    print(f"SQLite source:")
    print(f"  historical_prices:     {src_prices_n:>10,} rows ({src_tickers} tickers)")
    print(f"  fundamentals_snapshots: {src_funds_n:>10,} rows")

    with cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM historical_prices")
        dst_prices_n = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM fundamentals_snapshots")
        dst_funds_n = cur.fetchone()[0]
    print(f"Supabase before:")
    print(f"  historical_prices:     {dst_prices_n:>10,} rows")
    print(f"  fundamentals_snapshots: {dst_funds_n:>10,} rows")
    print()

    if args.dry_run:
        print("(dry run — no inserts)")
        return 0

    # ---- Build optional ticker filter ----
    where_clause = ""
    if args.limit_tickers:
        sample = [r[0] for r in src.execute(
            "SELECT DISTINCT ticker FROM historical_prices LIMIT ?",
            (args.limit_tickers,)
        )]
        placeholders = ",".join(f"'{t}'" for t in sample)
        where_clause = f"WHERE ticker IN ({placeholders})"
        print(f"Limited to {len(sample)} tickers: {sample}\n")

    # ---- Migrate historical_prices ----
    t0 = time.time()
    print("Migrating historical_prices...")
    rows = src.execute(f"""
        SELECT ticker, date, open, high, low, close, adj_close, volume
        FROM historical_prices {where_clause}
        ORDER BY ticker, date
    """).fetchall()
    inserted = _bulk_insert_prices(rows)
    print(f"  inserted/upserted {inserted:,} rows in {time.time()-t0:.1f}s")

    # ---- Migrate fundamentals_snapshots ----
    t0 = time.time()
    print("Migrating fundamentals_snapshots...")
    # Tag everything as akshare_annual since that's what's in SQLite today
    # (the daily yfinance snapshot cron is being killed in Stage 6).
    rows = src.execute(f"""
        SELECT ticker, snapshot_date,
               trailing_pe, forward_pe, price_to_book, ev_to_ebitda,
               dividend_yield, market_cap, beta, return_on_equity,
               debt_to_equity, last_price, currency, data_completeness,
               earnings_growth, revenue_growth, profit_margins,
               operating_margins, return_on_assets, current_ratio,
               free_cashflow, eps_ttm, bps, shares_outstanding
        FROM fundamentals_snapshots {where_clause}
    """).fetchall()
    inserted = _bulk_insert_fundamentals(rows)
    print(f"  inserted/upserted {inserted:,} rows in {time.time()-t0:.1f}s")

    # ---- Counts after + sanity ----
    print()
    with cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT ticker) FROM historical_prices")
        n, t = cur.fetchone()
        print(f"Supabase after:")
        print(f"  historical_prices:     {n:>10,} rows ({t} tickers)")
        cur.execute("SELECT COUNT(*) FROM fundamentals_snapshots")
        print(f"  fundamentals_snapshots: {cur.fetchone()[0]:>10,} rows")

    print()
    _sanity_check()
    return 0


def _bulk_insert_prices(rows: list) -> int:
    """Uses psycopg2.extras.execute_values which packs all params into a single
    multi-VALUES INSERT — ~100x faster than executemany over a remote DB."""
    from psycopg2.extras import execute_values
    sql = """
        INSERT INTO historical_prices
            (ticker, date, open, high, low, close, adj_close, volume)
        VALUES %s
        ON CONFLICT (ticker, date) DO NOTHING
    """
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i+BATCH_SIZE]
        params = [(r["ticker"], r["date"], r["open"], r["high"], r["low"],
                   r["close"], r["adj_close"], r["volume"]) for r in batch]
        with cursor() as cur:
            execute_values(cur, sql, params, page_size=BATCH_SIZE)
        total += len(batch)
        print(f"    {total:,}/{len(rows):,} prices...", flush=True)
    return total


def _bulk_insert_fundamentals(rows: list) -> int:
    from psycopg2.extras import execute_values
    cols = ["trailing_pe", "forward_pe", "price_to_book", "ev_to_ebitda",
            "dividend_yield", "market_cap", "beta", "return_on_equity",
            "debt_to_equity", "last_price", "currency", "data_completeness",
            "earnings_growth", "revenue_growth", "profit_margins",
            "operating_margins", "return_on_assets", "current_ratio",
            "free_cashflow", "eps_ttm", "bps", "shares_outstanding"]
    sql = f"""
        INSERT INTO fundamentals_snapshots
            (ticker, snapshot_date, source, {", ".join(cols)})
        VALUES %s
        ON CONFLICT (ticker, snapshot_date, source) DO NOTHING
    """
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i+BATCH_SIZE]
        params = [(r["ticker"], r["snapshot_date"], "akshare_annual",
                   *[r[c] for c in cols]) for r in batch]
        with cursor() as cur:
            execute_values(cur, sql, params, page_size=BATCH_SIZE)
        total += len(batch)
        print(f"    {total:,}/{len(rows):,} fundamentals...", flush=True)
    return total


def _sanity_check():
    """Pick 5 random tickers in the cloud DB, verify their row counts + a
    sample row exist."""
    print("Sanity check:")
    with cursor(dict_rows=True) as cur:
        cur.execute("""
            SELECT ticker, COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last
            FROM historical_prices
            GROUP BY ticker
            ORDER BY RANDOM()
            LIMIT 5
        """)
        for r in cur.fetchall():
            print(f"  {r['ticker']}: {r['n']:>5,} prices  ({r['first']} -> {r['last']})")


if __name__ == "__main__":
    sys.exit(main())
