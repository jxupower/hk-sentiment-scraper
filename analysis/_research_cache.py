"""Per-process cache for the universe-wide computation that powers the
Stock Research tab.

Each call to `build_research_report(ticker)` previously re-ran
`FactorScoringEngine.compute()` (scores ~2,769 tickers across 4 factors) AND
4 × `run_screen(...)` (each scans the full universe). That's ~5-9s of CPU/IO
paid per ticker load even when nothing about the universe changed between
loads.

This module memoizes both outputs in a thread-safe singleton with a TTL.
The cache is keyed by (db_path, sector_risk_path) so multiple dashboards
or test runs against different DBs don't collide.

Why a server-side cache and not a `dcc.Store`:
- A FactorResult is a dataclass with ~15 fields; 2,769 of them + 4 ScreenResult
  lists serialized to JSON each callback would itself cost hundreds of ms,
  defeating the win.
- A `dcc.Store` is per-browser-tab anyway; a server-side singleton is shared
  across all users/tabs of the dashboard process.

Invalidation: TTL only. The scheduler writes `fundamentals_snapshots` weekly
and `sentiment_scores` every 30 min; a 15-min TTL is loose enough that almost
all ticker loads within a session hit warm cache, and tight enough that newly
scraped sentiment shows up shortly after the next scrape cycle.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional

from analysis.factor_scores import EngineDiagnostics, FactorResult, FactorScoringEngine
from analysis.screens import BUILTIN_SCREENS, ScreenResult, run_screen


@dataclass
class _CacheBundle:
    factor_results: dict[str, FactorResult]
    factor_diagnostics: EngineDiagnostics
    screen_results_by_id: dict[str, list[ScreenResult]]
    built_at: datetime
    key: tuple


# Module-level state. Only mutated under `_lock`.
_lock = Lock()
_bundle: Optional[_CacheBundle] = None
_DEFAULT_TTL_SECONDS = 900  # 15 minutes


def get_or_build(db_path: str, sector_risk_path: Optional[str] = None,
                  *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> _CacheBundle:
    """Return the cached universe computation, building it under lock if
    missing, stale, or keyed to a different db/sector_risk path.

    Concurrent calls coalesce: if thread A is mid-build, threads B/C wait on
    the lock and reuse A's result instead of all three recomputing.
    """
    global _bundle
    key = (db_path, sector_risk_path)
    with _lock:
        if _is_fresh(key, ttl_seconds):
            return _bundle
        _bundle = _build(db_path, sector_risk_path, key)
        return _bundle


def invalidate() -> None:
    """Wipe the cache. Next get_or_build() will rebuild. Used by tests and
    optional UI refresh triggers."""
    global _bundle
    with _lock:
        _bundle = None


def _is_fresh(key: tuple, ttl_seconds: int) -> bool:
    if _bundle is None:
        return False
    if _bundle.key != key:
        return False
    age = datetime.now() - _bundle.built_at
    return age < timedelta(seconds=ttl_seconds)


def _build(db_path: str, sector_risk_path: Optional[str],
            key: tuple) -> _CacheBundle:
    engine = FactorScoringEngine(db_path, sector_risk_path)
    all_results, diagnostics = engine.compute()
    factor_by_ticker = {r.ticker: r for r in all_results}

    screen_results_by_id: dict[str, list[ScreenResult]] = {}
    for screen in BUILTIN_SCREENS:
        screen_results_by_id[screen.id] = run_screen(db_path, screen, sector_risk_path)

    return _CacheBundle(
        factor_results=factor_by_ticker,
        factor_diagnostics=diagnostics,
        screen_results_by_id=screen_results_by_id,
        built_at=datetime.now(),
        key=key,
    )
