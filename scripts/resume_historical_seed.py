"""Resume the interrupted bulk historical-price seed.

Background: a previous `main.py historical seed --tickers ALL --price-period 10y`
was killed at ticker 830/2,772 before reaching the yfinance price stage.
Result: only ~54 tickers have prices in the DB, the rest are blank.

This script:
  1. Reads the list of active tickers from local SQLite securities table
  2. Compares against the set already covered in Supabase historical_prices
  3. Skips tickers previously confirmed as having no yfinance data
     (data/.yfinance_delisted.txt, self-learned across runs)
  4. Fetches the missing tickers in batches via yfinance (existing scraper)
  5. Writes results to Supabase via CloudHistoricalPricesRepository
  6. Updates a checkpoint file every batch so re-runs are cheap
  7. Appends newly-confirmed no-data tickers to the delisted log

Long-running: ~6-8 hours for ~2,700 tickers due to yfinance throttling.
Run in background with nohup / a separate terminal.

Usage:
    python scripts/resume_historical_seed.py
    python scripts/resume_historical_seed.py --batch-size 50 --period 10y
    python scripts/resume_historical_seed.py --watchlist-only  # ~50 tickers, fast smoke
    python scripts/resume_historical_seed.py --limit 100        # for testing
"""
import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from scrapers.historical_price_scraper import fetch_many as price_fetch_many
from storage.cloud_db import available
from storage.cloud_repository import CloudHistoricalPricesRepository

CHECKPOINT_FILE = Path(__file__).parent.parent / "data" / ".seed_checkpoint.json"
# Self-learning skip list: tickers yfinance has confirmed as having no data
# (delisted, broken, or otherwise unreachable). Built up by fetch_many; loaded
# here to avoid re-probing on subsequent runs. NOT a static HKEX list — HKEX
# reuses 4-digit codes, so a third-party "delisted" list can wrongly exclude
# active tickers.
DELISTED_LOG = Path(__file__).parent.parent / "data" / ".yfinance_delisted.txt"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=50,
                     help="Tickers per yfinance batch call.")
    ap.add_argument("--period", default="10y",
                     help="yfinance period: 5y, 10y, max")
    ap.add_argument("--watchlist-only", action="store_true",
                     help="Skip the full universe; only seed the curated watchlist.")
    ap.add_argument("--limit", type=int, default=0,
                     help="Stop after seeding N tickers (0 = all).")
    ap.add_argument("--throttle-s", type=float, default=2.0,
                     help="Seconds to sleep between batches.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("seed")

    if not settings.cloud_db_configured() or not available():
        log.error("Cloud DB unavailable — set USE_CLOUD_DB=true + SUPABASE_DB_URL")
        return 1

    repo = CloudHistoricalPricesRepository()
    already_covered = set(repo.distinct_tickers())
    log.info("Supabase already has prices for %d tickers", len(already_covered))

    # Build target ticker list
    with sqlite3.connect(settings.DB_PATH) as conn:
        if args.watchlist_only:
            rows = conn.execute(
                "SELECT ticker FROM securities WHERE is_active=1 AND is_watchlist=1"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ticker FROM securities WHERE is_active=1"
            ).fetchall()
    all_tickers = sorted(r[0] for r in rows)
    missing = [t for t in all_tickers if t not in already_covered]

    # Filter out tickers yfinance has previously confirmed have no data.
    delisted = set()
    if DELISTED_LOG.exists():
        delisted = {ln.strip() for ln in DELISTED_LOG.read_text().splitlines()
                     if ln.strip()}
    if delisted:
        before = len(missing)
        missing = [t for t in missing if t not in delisted]
        log.info("Skipping %d tickers from %s (previously confirmed no-data)",
                 before - len(missing), DELISTED_LOG.name)

    if args.limit:
        missing = missing[:args.limit]

    log.info("Total active tickers: %d", len(all_tickers))
    log.info("Need to seed:        %d", len(missing))
    if not missing:
        log.info("Nothing to do — all tickers covered.")
        return 0

    # Resume from checkpoint if possible
    start_idx = 0
    if CHECKPOINT_FILE.exists():
        try:
            cp = json.loads(CHECKPOINT_FILE.read_text())
            if cp.get("missing_at_start") == missing:
                start_idx = cp.get("completed", 0)
                log.info("Resuming from checkpoint at index %d", start_idx)
        except Exception as e:
            log.warning("Checkpoint unreadable (%s); starting fresh", e)

    t_start = time.time()
    completed = start_idx
    for batch_start in range(start_idx, len(missing), args.batch_size):
        batch = missing[batch_start:batch_start + args.batch_size]
        log.info("Batch %d-%d / %d (%s ... %s)",
                 batch_start, batch_start + len(batch), len(missing),
                 batch[0], batch[-1])
        try:
            summary = price_fetch_many(batch, repo, period=args.period,
                                         verbose=True,
                                         delisted_log_path=DELISTED_LOG)
            log.info("  attempted=%d with_data=%d total_rows=%d failed=%d new_delisted=%d",
                     summary["attempted"], summary["tickers_with_data"],
                     summary["total_rows"], summary.get("failed_tickers", 0),
                     len(summary.get("newly_delisted", [])))
        except Exception as e:
            log.error("  batch failed: %s — continuing", e)

        completed = batch_start + len(batch)
        _write_checkpoint(missing, completed)
        time.sleep(args.throttle_s)

    elapsed = time.time() - t_start
    log.info("DONE — seeded %d tickers in %.1f minutes", completed, elapsed / 60)
    return 0


def _write_checkpoint(missing: list, completed: int):
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps({
        "missing_at_start": missing,
        "completed": completed,
    }))


if __name__ == "__main__":
    sys.exit(main())
