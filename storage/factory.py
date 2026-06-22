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


def get_prices_repo(db: Database):
    """Returns HistoricalPricesRepository (SQLite) or CloudHistoricalPricesRepository.
    Pass the local `Database` instance — it's used for the SQLite fallback path."""
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
