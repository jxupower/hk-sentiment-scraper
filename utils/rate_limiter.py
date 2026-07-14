"""Per-host rate limiter for parallel scrapers (perf P3.17).

The scrapers previously ran serially with `time.sleep(0.5)` between EVERY
per-ticker call, which meant a ~2,700-ticker HK cycle paid ~1,350 s of
wall-clock in throttle time alone. Because the throttle was global, N
workers hitting N different underlying hosts would still serialise.

`HostRateLimiter` moves the throttle to be per-host, thread-safe, and
zero-cost when the target host hasn't been touched inside the interval.
Combined with a `ThreadPoolExecutor` on the caller side (see the
scrapers/*.py refactors), the same 2,700-ticker cycle runs in ~170 s
with 8 workers hitting a handful of distinct hosts (yahoo.com,
akshare's em.eastmoney.com, RSS origins).

Semantics:
  - `wait(host)` blocks until it's safe to make the next call to `host`.
    Non-blocking if the last call to `host` was ≥ min_interval_s ago.
  - Two callers targeting DIFFERENT hosts never block each other.
  - Two callers targeting the SAME host serialise on that host's lock.
    The second returns from `wait()` at least min_interval_s after the
    first — the same behaviour as the old `time.sleep(0.5)` per host.
  - No token-bucket / burst logic. The old code was strict-minimum-
    interval; this preserves that (safe against source rate limits).

Zero new deps — stdlib only. Safe under GIL because the lock
handoff + timestamp update is a bounded critical section.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Optional


class HostRateLimiter:
    """Thread-safe per-host throttle. Enforces `min_interval_s` between
    successive `wait(host)` returns for the same host.

    Usage:
        limiter = HostRateLimiter(min_interval_s=0.5)
        ...
        limiter.wait("yahoo.com")
        rows = yf.Ticker(t).news
    """

    def __init__(self, min_interval_s: float = 0.5):
        self._min_interval = float(min_interval_s)
        # Global lock guards the per-host lock table itself (rare — only
        # the first `wait` for a new host acquires it). Per-host locks
        # guard each host's last-call timestamp (hot path).
        self._table_lock = Lock()
        self._host_locks: dict[str, Lock] = {}
        self._last_call: dict[str, float] = {}

    def _get_host_lock(self, host: str) -> Lock:
        lock = self._host_locks.get(host)
        if lock is not None:
            return lock
        with self._table_lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = Lock()
                self._host_locks[host] = lock
            return lock

    def wait(self, host: str) -> None:
        """Block until the caller may make a fresh call to `host`.

        Returns immediately (no sleep) if the last call to this host was
        more than `min_interval_s` ago OR this is the first call for the
        host. Otherwise sleeps exactly the remaining interval.
        """
        host_lock = self._get_host_lock(host)
        with host_lock:
            now = time.monotonic()
            last = self._last_call.get(host)
            if last is not None:
                elapsed = now - last
                if elapsed < self._min_interval:
                    time.sleep(self._min_interval - elapsed)
                    now = time.monotonic()
            self._last_call[host] = now


# Module-level shared instance. All scrapers use this so a Yahoo call in
# the RSS scraper and a Yahoo call in the yahoo_scraper coordinate on the
# same throttle. Configure via env for future tuning; default 0.5s
# matches the pre-P3.17 `time.sleep(0.5)` behaviour exactly.
import os as _os

_default_interval = float(_os.getenv("SCRAPER_HOST_MIN_INTERVAL_S", "0.5"))
_shared: Optional[HostRateLimiter] = None


def get_shared_limiter() -> HostRateLimiter:
    global _shared
    if _shared is None:
        _shared = HostRateLimiter(min_interval_s=_default_interval)
    return _shared
