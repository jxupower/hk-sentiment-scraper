"""Loader for `config/index_constituents.yaml` — the cached Wikipedia-scraped
(+ hand-curated for HSTECH) mapping of major-index symbol → constituent
ticker list.

Used by the Market tab (dashboard/market_layout.py) to filter the universe
fundamentals table to the holdings of the selected index.

Public API:
    load_index_constituents() -> dict[str, dict]
        Full {symbol: {name, market, source_url, updated, tickers}} map.
    constituents_for(symbol: str) -> list[str]
        Just the ticker list for one index, [] if unknown.
    index_meta(symbol: str) -> dict | None
        The full entry for one index, None if unknown — for the "last
        refreshed" timestamp the UI shows beneath the table.

Module-level cache: the YAML is parsed once on first call and pinned. Re-
running `python scripts/refresh_index_constituents.py` updates the file on
disk but the Dash process won't re-read it until restart. Acceptable —
constituents reconstitute quarterly, not daily.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


_YAML_PATH = Path(__file__).resolve().parent / "index_constituents.yaml"


@lru_cache(maxsize=1)
def load_index_constituents() -> dict[str, dict]:
    """Parse the YAML once and pin in-process. Returns {} if the file is
    missing or empty — callers should degrade gracefully (chart still
    works, table shows "constituent list not maintained")."""
    if not _YAML_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return data


def constituents_for(symbol: str) -> list[str]:
    """Return the ticker list for an index symbol (e.g. "^HSI", "^GSPC").
    Returns [] when the index isn't in the YAML — the Market tab will
    render the chart anyway and show a "no constituents" placeholder
    where the table would normally be."""
    entry = load_index_constituents().get(symbol)
    if not entry:
        return []
    tickers = entry.get("tickers") or []
    # De-dup preserving order (the HSTECH hand-curated list deliberately
    # contains one repeat for the loader to absorb — keeps the source-of-
    # truth YAML auditable per change).
    seen = set()
    out: list[str] = []
    for t in tickers:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def index_meta(symbol: str) -> dict | None:
    """Full entry (name + market + source_url + updated date) for one index."""
    return load_index_constituents().get(symbol)
