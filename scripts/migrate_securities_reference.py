"""Apply the `securities_reference` Supabase schema + one-shot copy of the
currently-resolved bilingual names + sector taxonomy from local SQLite up
to Supabase.

Run once after deploying the cloud table:

    python scripts/migrate_securities_reference.py

Idempotent — re-running just no-ops the ON CONFLICT clauses.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from storage.database import Database  # noqa: E402
from analysis.data_loader import (  # noqa: E402
    push_securities_reference,
    refresh_securities_reference_cache,
)
from utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    # 1. Apply Supabase schema (idempotent — runs the full schema SQL)
    try:
        from storage.cloud_db import available, connection
    except Exception as e:
        print(f"Supabase pool not importable ({e}); aborting.")
        sys.exit(1)
    if not available():
        print("USE_CLOUD_DB != true or pool unavailable — aborting.")
        sys.exit(1)

    schema_sql = (
        Path(__file__).parent / "supabase_schema.sql"
    ).read_text(encoding="utf-8")
    print("Applying Supabase schema (idempotent)...")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()
    print("  OK.")

    # 2. Bulk-push current local rows up to Supabase
    import config.settings as s
    db = Database(s.DB_PATH)
    db.initialize()

    print("\nPushing local securities + names → Supabase.securities_reference ...")
    push_summary = push_securities_reference(db)
    print(f"  pushed: {push_summary['pushed']:,} rows in {push_summary['elapsed_s']:.1f}s")

    # 3. Pull back into local mirror (fresh updated_at stamps)
    print("\nRefreshing local mirror from Supabase ...")
    pull_summary = refresh_securities_reference_cache(db)
    print(f"  fetched: {pull_summary['fetched']:,}  "
          f"written: {pull_summary['written']:,}  "
          f"elapsed: {pull_summary['elapsed_s']:.1f}s")

    # 4. Sanity counts
    from storage.cloud_repository import CloudSecuritiesReferenceRepository
    from storage.repository import SecuritiesReferenceRepository
    n_cloud = CloudSecuritiesReferenceRepository().count()
    n_local = SecuritiesReferenceRepository(db).count()
    print(f"\nFinal counts — Supabase: {n_cloud:,}  ·  local: {n_local:,}")
    if n_cloud == n_local:
        print("✓ in sync")
    else:
        print(f"⚠ mismatch ({abs(n_cloud - n_local)} rows different)")


if __name__ == "__main__":
    main()
