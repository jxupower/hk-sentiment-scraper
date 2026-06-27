"""One-off: backfill `dividend_yield` on the latest US fundamentals snapshot.

Why this is its own script: the akshare US scraper never populates
dividend_yield (Eastmoney's endpoint doesn't return it) and the daily
yfinance .info cron has been disabled to stay inside Supabase's free-tier
budget. Net result: every US row in the Screener shows '—' for yield.

This script targets ONLY the dividend_yield column on each US ticker's
most recent snapshot row — it UPDATEs in place rather than inserting a
new daily row, so it adds ~0 storage to Supabase. Resume-safe: skips
tickers whose latest snapshot already has a non-null dividend_yield, so
re-running picks up where a crash / network hiccup left off.

Usage:
    venv\\Scripts\\python scripts\\patch_us_dividend_yield.py
        [--throttle 1.5]     # seconds between yfinance calls
        [--limit N]          # cap for testing; omit for full universe
        [--log-every 25]     # progress cadence
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Make project root importable when running this file directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv(override=True)
if os.environ.get("USE_CLOUD_DB", "").lower() != "true":
    os.environ["USE_CLOUD_DB"] = "true"

import yfinance as yf

from storage import cloud_db
from utils.logger import get_logger

logger = get_logger(__name__)


def _coerce_finite(v):
    import math
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def fetch_targets(limit: int | None) -> list[tuple[str, str, str]]:
    """Tickers whose latest US snapshot is missing dividend_yield, along with
    the composite-PK fields `(snapshot_date, source)` for the targeted row.
    Resume-safe filter — re-running picks up where a crash left off.

    Schema note: Supabase `fundamentals_snapshots` uses a composite PK of
    (ticker, snapshot_date, source); there is no `id` column."""
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (ticker) ticker, snapshot_date, source,
                                          dividend_yield
            FROM fundamentals_snapshots
            WHERE market = 'US'
            ORDER BY ticker, snapshot_date DESC
        )
        SELECT ticker, snapshot_date, source
        FROM latest
        WHERE dividend_yield IS NULL
        ORDER BY ticker
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with cloud_db.cursor(dict_rows=True) as cur:
        cur.execute(sql)
        return [(r["ticker"], r["snapshot_date"], r["source"])
                for r in cur.fetchall()]


def fetch_yield(ticker: str) -> float | None:
    """yfinance .info → dividendYield (percent units, e.g. 3.28 for 3.28%).
    Returns None when yfinance fails or the ticker pays no dividend."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        logger.warning("yfinance .info failed [%s]: %s", ticker, e)
        return None
    return _coerce_finite(info.get("dividendYield"))


def update_yield(ticker: str, snapshot_date, source: str, dy: float) -> None:
    """UPDATE one snapshot row's dividend_yield in place, keyed by composite PK."""
    with cloud_db.cursor() as cur:
        cur.execute(
            "UPDATE fundamentals_snapshots SET dividend_yield = %s "
            "WHERE ticker = %s AND snapshot_date = %s AND source = %s",
            (dy, ticker, snapshot_date, source),
        )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--throttle", type=float, default=1.5,
                    help="Seconds between yfinance calls (default 1.5).")
    p.add_argument("--limit", type=int, default=None,
                    help="Cap to first N tickers; omit for full universe.")
    p.add_argument("--log-every", type=int, default=25,
                    help="Progress log cadence (default every 25 tickers).")
    args = p.parse_args()

    if not cloud_db.available():
        sys.exit("[fatal] Supabase not reachable. Check USE_CLOUD_DB + SUPABASE_DB_URL.")

    targets = fetch_targets(args.limit)
    total = len(targets)
    if total == 0:
        print("Nothing to do — every US latest snapshot already has dividend_yield.")
        return

    print(f"Patching dividend_yield on {total} US tickers "
          f"(throttle={args.throttle}s, est ~{total * args.throttle / 60:.0f} min)")

    written, no_div, failed = 0, 0, 0
    t0 = time.time()
    for i, (ticker, snap_date, source) in enumerate(targets, start=1):
        dy = fetch_yield(ticker)
        if dy is None:
            # Could be: yfinance failure OR the ticker truly pays no dividend.
            # We can't tell the two apart from the .info response, so we count
            # them as "no_div" and don't retry. Re-running the script will pick
            # them up again — cheap because the resume filter only checks
            # `dividend_yield IS NULL`.
            no_div += 1
        else:
            try:
                update_yield(ticker, snap_date, source, dy)
                written += 1
            except Exception as e:
                logger.warning("UPDATE failed [%s %s/%s]: %s",
                                ticker, snap_date, source, e)
                failed += 1

        if i % args.log_every == 0:
            elapsed = time.time() - t0
            rate = i / elapsed
            eta_min = (total - i) / rate / 60 if rate > 0 else 0
            print(f"  [{i}/{total}] written={written} no_div={no_div} "
                  f"failed={failed} | rate={rate:.2f}/s eta={eta_min:.0f}m")

        time.sleep(args.throttle)

    print()
    print(f"Done. written={written} no_div={no_div} failed={failed} "
          f"(elapsed {(time.time() - t0) / 60:.1f} min)")


if __name__ == "__main__":
    main()
