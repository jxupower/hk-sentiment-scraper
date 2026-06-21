"""Idempotent migration: add `market` column to securities / historical_prices
/ fundamentals_snapshots / articles, backfill by ticker convention, drop the
legacy NOT NULL constraint on securities.hkex_code so US rows can be inserted.

Safe to re-run — every step probes current state before touching anything.

Local SQLite:    runs the Database.initialize() migration block (which now
                 owns this logic).
Supabase:        runs scripts/supabase_schema.sql (its IF NOT EXISTS / ADD
                 COLUMN IF NOT EXISTS blocks are idempotent).

Usage:
    python scripts/migrate_add_market_column.py
    python scripts/migrate_add_market_column.py --skip-cloud
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.database import Database  # noqa: E402
from utils.logger import get_logger    # noqa: E402

logger = get_logger(__name__)

DEFAULT_SQLITE_PATH = "data/sentiment.db"


def run_sqlite(db_path: str) -> dict:
    """Run the migration against local SQLite and return per-market row counts."""
    logger.info("Migrating SQLite at %s ...", db_path)
    db = Database(db_path)
    db.initialize()
    with db.get_connection() as conn:
        counts = {}
        for table in ("securities", "historical_prices",
                      "fundamentals_snapshots", "articles"):
            try:
                rows = conn.execute(
                    f"SELECT market, COUNT(*) FROM {table} GROUP BY market"
                ).fetchall()
                counts[table] = {r[0]: r[1] for r in rows}
            except Exception as e:
                counts[table] = f"<query failed: {e}>"
    return counts


def run_supabase() -> dict | None:
    """Run the schema SQL against the Supabase Postgres pool."""
    if os.environ.get("USE_CLOUD_DB", "").lower() != "true":
        logger.info("USE_CLOUD_DB != true; skipping Supabase migration")
        return None
    try:
        from storage.cloud_db import connection, available
    except Exception as e:
        logger.warning("Supabase module not importable (%s); skipping cloud migration", e)
        return None

    if not available():
        logger.warning("Supabase pool not available; skipping cloud migration")
        return None

    schema_path = Path(__file__).parent / "supabase_schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")
    logger.info("Running Supabase schema migration (%s bytes) ...", len(schema_sql))
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()
        counts = {}
        with conn.cursor() as cur:
            for table in ("historical_prices", "fundamentals_snapshots"):
                cur.execute(
                    f"SELECT market, COUNT(*) FROM {table} GROUP BY market"
                )
                counts[table] = {r[0]: r[1] for r in cur.fetchall()}
    return counts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sqlite-path", default=DEFAULT_SQLITE_PATH,
                   help=f"SQLite DB path (default: {DEFAULT_SQLITE_PATH})")
    p.add_argument("--skip-cloud", action="store_true",
                   help="Skip Supabase migration even if USE_CLOUD_DB=true")
    args = p.parse_args()

    sqlite_counts = run_sqlite(args.sqlite_path)
    print("\n=== SQLite row counts per market ===")
    for table, counts in sqlite_counts.items():
        print(f"  {table}: {counts}")

    if not args.skip_cloud:
        cloud_counts = run_supabase()
        if cloud_counts is not None:
            print("\n=== Supabase row counts per market ===")
            for table, counts in cloud_counts.items():
                print(f"  {table}: {counts}")

    print("\nMigration complete.")


if __name__ == "__main__":
    main()
