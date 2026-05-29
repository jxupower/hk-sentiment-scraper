"""Cumulative Average Growth Rate (CAGR) helpers.

CAGR = (end_value / start_value)^(1/years) - 1

Plain Bagel's step 3 advocates computing CAGR over multiple horizons (5/10/15y)
for revenue, earnings, BPS, dividends — gives a richer picture of growth
trajectory than a single point-in-time YoY number.

For our HK data (akshare gives ~9 years of annual), 15y CAGR is rarely
computable; we gracefully return None when insufficient history exists.
"""
import math
from typing import Optional


def compute_cagr(start_value: Optional[float], end_value: Optional[float],
                 years: int) -> Optional[float]:
    """CAGR between two values over `years` periods.

    Returns None if any input is invalid, non-positive (CAGR undefined for
    sign changes), or if years <= 0. Returns the result as a fraction
    (0.10 = 10% annualized)."""
    if start_value is None or end_value is None or years <= 0:
        return None
    try:
        s = float(start_value)
        e = float(end_value)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(s) and math.isfinite(e)):
        return None
    # CAGR is undefined when sign changes or values are non-positive
    if s <= 0 or e <= 0:
        return None
    try:
        return (e / s) ** (1 / years) - 1
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def multi_horizon_cagr(history_sorted_oldest_first: list[tuple[str, Optional[float]]],
                       horizons: list[int] = (5, 10, 15)) -> dict[int, Optional[float]]:
    """Compute CAGR at multiple horizons from a chronologically-sorted (date, value)
    list. Uses the LATEST value as endpoint and the value `horizon` years earlier
    as startpoint (looked up by date proximity, not strict year arithmetic).

    Returns {horizon: CAGR or None}. None means insufficient history or
    sign-change."""
    if not history_sorted_oldest_first:
        return {h: None for h in horizons}

    # Use the last (most recent) value as the endpoint
    end_date, end_value = history_sorted_oldest_first[-1]

    # For each horizon, find the value approximately N years back
    out: dict[int, Optional[float]] = {}
    for h in horizons:
        # Index lookup: take the row `h` positions before the end (data is annual)
        start_idx = len(history_sorted_oldest_first) - 1 - h
        if start_idx < 0:
            out[h] = None
            continue
        _, start_value = history_sorted_oldest_first[start_idx]
        out[h] = compute_cagr(start_value, end_value, h)
    return out


def yoy_growth_series(history_sorted_oldest_first: list[tuple[str, Optional[float]]]
                      ) -> list[tuple[str, Optional[float]]]:
    """Convert a value series to YoY growth fractions. First entry returns None."""
    out: list[tuple[str, Optional[float]]] = []
    for i, (date, value) in enumerate(history_sorted_oldest_first):
        if i == 0:
            out.append((date, None))
            continue
        _, prev = history_sorted_oldest_first[i - 1]
        if prev is None or value is None or prev == 0:
            out.append((date, None))
            continue
        try:
            out.append((date, (float(value) - float(prev)) / float(prev)))
        except (TypeError, ValueError, ZeroDivisionError):
            out.append((date, None))
    return out
