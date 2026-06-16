"""Shared preset-filter logic used by both the Screener tab callbacks and the
Backtest engine.

A preset (see `dashboard/screener_presets.py:INVESTOR_PRESETS`) is a dict of
slider-range overrides keyed by `NUMERIC_FILTERS` slug. To apply it to a row
of fundamentals, we walk each constraint, transform the raw row value into
the slider's display units (e.g. ROE 0.18 → 18.0%, market_cap → billions),
and bound-check it. Rows with missing values for a constrained field pass —
matches the Screener's permissive "unknown ≠ excluded" semantics. This is
what the Screener has always done; the backtest needs the same behaviour
so results are consistent with what users see when they click a preset.
"""
from typing import Optional


def _xform_value(xform_key: Optional[str], raw):
    """Adapt the raw row value to the slider's display units. Mirrors the
    private `_xform_value` in dashboard/screener_callbacks.py — kept in sync
    by sharing this module."""
    if raw is None:
        return None
    if xform_key == "roe_pct":
        return raw * 100
    if xform_key == "mcap_b":
        return raw / 1e9 if raw else None
    return raw


def _in_range(v, lo, hi) -> bool:
    """A row passes if value is missing (treat as 'unknown' — don't exclude)
    OR falls inside [lo, hi]."""
    if v is None:
        return True
    try:
        f = float(v)
    except (TypeError, ValueError):
        return True
    return lo <= f <= hi


def passes_preset(row: dict, preset: dict, numeric_filters: list) -> bool:
    """True iff `row` satisfies all constrained slider ranges in `preset`.

    `preset` is one element of `INVESTOR_PRESETS` — its "sliders" dict maps
    slug → [lo, hi]. `numeric_filters` is `NUMERIC_FILTERS` from
    dashboard/screener_layout.py — the slug → (row_key, xform) table.
    """
    overrides = preset.get("sliders") or {}
    if not overrides:
        return True
    for _label, slug, _lo, _hi, _step, row_key, xform in numeric_filters:
        bounds = overrides.get(slug)
        if not bounds:
            continue
        lo, hi = bounds[0], bounds[1]
        if not _in_range(_xform_value(xform, row.get(row_key)), lo, hi):
            return False
    return True


def apply_preset(rows: list[dict], preset: dict, numeric_filters: list) -> list[dict]:
    """Filter `rows` to the subset that passes all of `preset`'s constraints."""
    return [r for r in rows if passes_preset(r, preset, numeric_filters)]
