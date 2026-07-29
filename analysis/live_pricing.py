"""Live-price recomputation of price-dependent ratios (P/E, P/B).

Fundamentals snapshots store `trailing_pe = last_price_at_fetch / eps_ttm`
and `price_to_book = last_price_at_fetch / bps` — both frozen at the moment
of the akshare/yfinance fetch. Once the market moves, those ratios drift
away from reality (dramatically for volatile tickers — Laopu Gold 6181.HK
dropped -38% between its 2026-05-27 snapshot and 2026-07-28, but its stored
P/E hadn't budged).

`live_pe` / `live_pb` derive the current ratio from a per-share denominator
× the current price. When the snapshot doesn't carry `eps_ttm` / `bps`
directly (the akshare HK path leaves them NULL and only stores the frozen
ratio + frozen price), the helpers back-derive the per-share value from
`snapshot.last_price / snapshot.trailing_pe`. That's algebraically exact —
if the snapshot's ratio + price are consistent, we recover the per-share
value the ratio was originally computed against.

Returns None (never a stale or fabricated value) whenever any input is
missing or non-positive. Callers should fall back to the snapshot's
stored ratio in that case rather than treat None as "0 P/E".
"""
from __future__ import annotations

import math
from typing import Optional


def _finite(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _derive_per_share(snapshot: dict, ratio_field: str,
                      per_share_field: str) -> Optional[float]:
    """Return the stored per-share value if present + positive, else
    back-derive it from `snapshot[<ratio_field>] × snapshot.last_price`."""
    ps = _finite(snapshot.get(per_share_field))
    if ps is not None and ps != 0:
        return ps
    ratio = _finite(snapshot.get(ratio_field))
    last_price = _finite(snapshot.get("last_price"))
    if ratio is None or last_price is None or ratio <= 0:
        return None
    return last_price / ratio


def live_pe(snapshot: dict, current_price: Optional[float]) -> Optional[float]:
    """Compute a live trailing-P/E from a fundamentals snapshot + the
    current market price.

    Preference order:
      1. `eps_ttm` × current_price  (cleanest — no back-derivation)
      2. back-derived eps_ttm = `last_price / trailing_pe`, then × current_price

    Returns None if we can't produce a positive finite result.
    """
    cp = _finite(current_price)
    if cp is None or cp <= 0:
        return None
    eps = _derive_per_share(snapshot, "trailing_pe", "eps_ttm")
    if eps is None or eps == 0:
        return None
    try:
        return cp / eps
    except ZeroDivisionError:
        return None


def live_pb(snapshot: dict, current_price: Optional[float]) -> Optional[float]:
    """Compute a live P/B from a fundamentals snapshot + current price.
    Mirrors `live_pe` but derives book-value-per-share from `bps` /
    back-derives from `last_price / price_to_book`."""
    cp = _finite(current_price)
    if cp is None or cp <= 0:
        return None
    bps = _derive_per_share(snapshot, "price_to_book", "bps")
    if bps is None or bps == 0:
        return None
    try:
        return cp / bps
    except ZeroDivisionError:
        return None


_SENTINEL = "_live_ratios_patched"


def patch_row_with_live_ratios(row: dict, current_price: Optional[float]) -> dict:
    """In-place patch: overwrite `trailing_pe` and `price_to_book` on a
    universe row with live values when they can be computed. When either
    live value can't be derived, the stored value is kept (never replaced
    with None) so downstream code that reads the field still sees the
    original snapshot value rather than a regression to null.

    IDEMPOTENT — a row that has already been patched is left alone on the
    second call. Without this, a caller upstream (research_orchestrator)
    patching the current ticker's row + a caller downstream (peer_comparison)
    also patching it would back-derive the per-share value from the ALREADY-
    LIVE ratio × the stale snapshot.last_price, producing a nonsense
    doubled-up value (observed: 6181.HK's target-cell in the peer scorecard
    coming out at 5.70 instead of the correct 9.33 after the second pass).

    Returns the same row for chaining. Does not touch any other fields."""
    if current_price is None:
        return row
    if row.get(_SENTINEL):
        return row
    live_p_e = live_pe(row, current_price)
    if live_p_e is not None:
        row["trailing_pe"] = live_p_e
    live_p_b = live_pb(row, current_price)
    if live_p_b is not None:
        row["price_to_book"] = live_p_b
    row[_SENTINEL] = True
    return row
