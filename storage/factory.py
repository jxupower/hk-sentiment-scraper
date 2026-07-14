"""Repository factory — returns SQLite or Postgres-backed repos based on env.

The toggle is `USE_CLOUD_DB` in `.env`:
- "true"  → returns CloudHistoricalPricesRepository / CloudFundamentalsRepository
            (talks to Supabase Postgres via storage/cloud_db.py)
- "false" → returns the original SQLite-backed repos from storage/repository.py

Two-table-only migration: only historical_prices and fundamentals_snapshots
have cloud variants. All other repos (articles, sentiment, signals, securities,
research_notes, backtest_*) are SQLite-only and continue to be constructed
directly from `storage/repository.py`.

Fallback behavior: if USE_CLOUD_DB=true but Supabase is unreachable at startup,
factory falls back to SQLite with a warning. This keeps the dashboard usable
when the network is flaky.
"""
import logging
from typing import Union

from config import settings
from storage.database import Database
from storage.repository import (
    HistoricalPricesRepository,
    FundamentalsRepository,
)

log = logging.getLogger(__name__)

_warned_cloud_unavailable = False


def _cloud_ok() -> bool:
    """Lazy + cached check — runs the ping once on first call. Subsequent
    calls reuse the connection pool's state."""
    global _warned_cloud_unavailable
    if not settings.cloud_db_configured():
        return False
    try:
        from storage.cloud_db import available
        ok = available()
        if not ok and not _warned_cloud_unavailable:
            log.warning("USE_CLOUD_DB=true but Supabase pool unavailable — "
                         "falling back to local SQLite for prices+fundamentals.")
            _warned_cloud_unavailable = True
        return ok
    except Exception as e:
        if not _warned_cloud_unavailable:
            log.warning("Cloud DB unavailable (%s) — falling back to SQLite.", e)
            _warned_cloud_unavailable = True
        return False


_warned_parquet_missing = False


def _use_parquet_prices() -> bool:
    """Perf P3.16 routing check. Returns True iff the local Parquet
    store has been populated (≥100 ticker dirs) AND the operator hasn't
    forced cloud fallback via `USE_PARQUET_PRICES=false`.

    Cheap — only scans the top-level directory of `data/prices/`. Not
    cached across calls because the store gets populated by an operator-
    triggered migration, and we want the flip to be picked up on the
    very next call.
    """
    global _warned_parquet_missing
    import os
    override = os.getenv("USE_PARQUET_PRICES", "").lower()
    if override == "false":
        return False
    try:
        from storage.parquet_prices import store_populated
        ok = store_populated()
        if not ok and override == "true" and not _warned_parquet_missing:
            log.warning("USE_PARQUET_PRICES=true but data/prices/ is empty — "
                          "falling back to Supabase for historical_prices.")
            _warned_parquet_missing = True
        return ok
    except Exception as e:
        if not _warned_parquet_missing:
            log.warning("Parquet prices routing check failed (%s) — using "
                          "Supabase fallback.", e)
            _warned_parquet_missing = True
        return False


def get_prices_repo(db: Database):
    """Returns the highest-tier available historical-prices repo:

      1. `ParquetHistoricalPricesRepository` (local Parquet, perf P3.16)
         when `data/prices/` is populated
      2. `CloudHistoricalPricesRepository` (Supabase) when USE_CLOUD_DB is
         on and the Supabase pool is reachable
      3. SQLite `HistoricalPricesRepository` as ultimate fallback

    All three implement the same public interface, so callers don't need
    to know which one they got.
    """
    if _use_parquet_prices():
        from storage.parquet_prices import ParquetHistoricalPricesRepository
        return ParquetHistoricalPricesRepository()
    if _cloud_ok():
        from storage.cloud_repository import CloudHistoricalPricesRepository
        return CloudHistoricalPricesRepository()
    return HistoricalPricesRepository(db)


def get_fundamentals_repo(db: Database):
    if _cloud_ok():
        from storage.cloud_repository import CloudFundamentalsRepository
        return CloudFundamentalsRepository()
    return FundamentalsRepository(db)


def get_securities_reference_repo(db: Database):
    """Returns CloudSecuritiesReferenceRepository or SecuritiesReferenceRepository.
    Used by `analysis/data_loader.refresh_securities_reference_cache` (to pull
    cloud rows into local SQLite) and `push_securities_reference` (to write
    the reconciler's resolved sectors + names back up). Dashboard read sites
    always go through the LOCAL repo for sub-millisecond response — the
    cloud version is only touched by the sync helpers."""
    if _cloud_ok():
        from storage.cloud_repository import CloudSecuritiesReferenceRepository
        return CloudSecuritiesReferenceRepository()
    from storage.repository import SecuritiesReferenceRepository
    return SecuritiesReferenceRepository(db)


def get_local_securities_reference_repo(db: Database):
    """Always returns the SQLite mirror — bypasses the cloud router.
    Dashboard reads go through here for sub-ms latency."""
    from storage.repository import SecuritiesReferenceRepository
    return SecuritiesReferenceRepository(db)
