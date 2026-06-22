"""Sub-sector composite tickers — `&NAME` synthetic indices.

Materialises one cap-weighted, chain-linked Laspeyres index per distinct
`securities.sub_sector` value, written to `historical_prices` under a
`&NAME` ticker (e.g. `&SEMICONDUCTORS_AND_EQUIPMENT`). The Research and
Risk Forecast tabs surface these as first-class tickers — Research shows
a sub-sector dashboard (constituents + aggregate stats + price chart);
Risk Forecast just runs GARCH on the series like any other ticker.

Methodology — chain-linked Laspeyres, the same family used by HSI / S&P:

  For each date t:
    common   = constituents with non-null adj_close on BOTH (t-1) and t
    mcap_t   = Σᵢ sharesᵢ × adj_closeᵢ(t)     for i in common
    mcap_tm1 = Σᵢ sharesᵢ × adj_closeᵢ(t-1)   for i in common
    return_t = mcap_t / mcap_tm1 - 1
    index(t) = index(t-1) × (1 + return_t)

The "common with previous" restriction is the chain-link trick: a
constituent that joins mid-history doesn't cause an artificial value jump
because it's not in `common_prev` on its first traded day. From day 2
onwards its mcap participates normally.

`sharesᵢ` = the latest non-null `shares_outstanding` from
`fundamentals_snapshots`. Constituent membership = current snapshot of
`securities.sub_sector` for all of history (documented limitation).

Mirrors the `@portfolio` synthetic-ticker conventions in
`analysis/portfolio_synth.py` — same upsert path, same staleness check,
same data-loader prefix routing.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from typing import Optional

import pandas as pd

from analysis.portfolio_synth import _series_to_price_rows
from storage.database import Database
from storage.factory import get_prices_repo

log = logging.getLogger(__name__)


SUBSECTOR_PREFIX = "&"
# Longer than the @portfolio cap of 32 because real sub-sector names get
# long when normalised — "Platforms & Cloud Infrastructure" is 34 chars.
SUBSECTOR_NAME_MAX_LEN = 40
NAME_PATTERN = re.compile(r"^[A-Z0-9_]{1,%d}$" % SUBSECTOR_NAME_MAX_LEN)

# Same staleness budget as @portfolios — composites refresh daily via the
# cron, but the cache-aside loader rebuilds on miss if anything slipped.
SUBSECTOR_STALE_DAYS = 1


def normalise_subsector_name(label: str) -> str:
    """'Semiconductors & Equipment' → 'SEMICONDUCTORS_AND_EQUIPMENT'.
    Replaces `&` with AND, hyphens / whitespace with `_`, strips other
    punctuation, uppercases, truncates to `SUBSECTOR_NAME_MAX_LEN`. Stable
    and deterministic — same input always produces the same output."""
    if not label:
        return ""
    s = label.strip()
    s = s.replace("&", " AND ")
    s = re.sub(r"[\-/]+", " ", s)
    s = re.sub(r"[^A-Za-z0-9 ]+", "", s)
    s = re.sub(r"\s+", "_", s.strip()).upper()
    return s[:SUBSECTOR_NAME_MAX_LEN]


def is_valid_normalised_name(name: str) -> bool:
    return bool(NAME_PATTERN.match(name or ""))


def to_subsector_ticker(label_or_name: str, market: Optional[str] = None) -> str:
    """Accept either the human label or the already-normalised slug.
    Returns the composite ticker string.

    When `market` is set, the ticker is namespaced — `&HK:BANKS` /
    `&US:BANKS` — so HK and US sub-sectors of the same name compute and
    cache independently in `historical_prices`. When `market` is None,
    returns the legacy un-namespaced form `&BANKS` (which the codebase
    treats as HK for backwards compatibility).
    """
    n = (label_or_name or "").strip()
    if n.startswith(SUBSECTOR_PREFIX):
        n = n[1:]
        # Strip an already-present namespace so we re-apply consistently.
        if ":" in n:
            n = n.split(":", 1)[1]
    if not is_valid_normalised_name(n):
        n = normalise_subsector_name(label_or_name)
    if market:
        return f"{SUBSECTOR_PREFIX}{market.upper()}:{n}"
    return f"{SUBSECTOR_PREFIX}{n}"


def parse_subsector_ticker(ticker: str) -> Optional[str]:
    """Return the normalised slug (without `&` and without market namespace)
    or None if not a composite. Accepts both forms — `&BANKS` and
    `&US:BANKS` map to slug `BANKS`."""
    if not ticker or not ticker.startswith(SUBSECTOR_PREFIX):
        return None
    name = ticker[1:]
    if ":" in name:
        name = name.split(":", 1)[1]
    if not is_valid_normalised_name(name):
        return None
    return name


def parse_subsector_market(ticker: str) -> str:
    """Extract the market from a namespaced composite ticker. Returns 'HK'
    for the legacy un-namespaced form so existing `&NAME` rows continue to
    resolve correctly."""
    if not ticker or not ticker.startswith(SUBSECTOR_PREFIX):
        return "HK"
    body = ticker[1:]
    if ":" in body:
        return body.split(":", 1)[0].upper()
    return "HK"


def is_subsector_stale(latest_date_str: Optional[str]) -> bool:
    if not latest_date_str:
        return True
    try:
        latest = datetime.strptime(latest_date_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return True
    return (date.today() - latest).days > SUBSECTOR_STALE_DAYS


# ============== Constituent + share lookups ==============

def _active_constituents(db: Database, sub_sector_label: str,
                          market: Optional[str] = None) -> list[str]:
    """All active `securities` rows with the supplied sub_sector. Matches
    on the original human label stored in `securities.sub_sector`. When
    `market` is set, restricts to that market — required so a US `Banks`
    composite doesn't include HK banks (and vice versa)."""
    with db.get_connection() as conn:
        if market is None:
            rows = conn.execute(
                "SELECT ticker FROM securities "
                "WHERE is_active = 1 AND sub_sector = ? "
                "ORDER BY ticker",
                (sub_sector_label,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ticker FROM securities "
                "WHERE is_active = 1 AND sub_sector = ? AND market = ? "
                "ORDER BY ticker",
                (sub_sector_label, market.upper()),
            ).fetchall()
    return [r[0] for r in rows]


def _latest_shares_outstanding(db: Database,
                                  tickers: list[str]) -> dict[str, float]:
    """{ticker: latest implied shares_outstanding}. Reads from the universe
    fundamentals row; falls back to `market_cap / last_price` when the
    explicit shares column is NULL (which it is for most akshare snapshots
    — only yfinance-sourced rows populate it directly)."""
    if not tickers:
        return {}
    from analysis.data_loader import get_universe_fundamentals
    rows = get_universe_fundamentals(db)
    by_t = {r["ticker"]: r for r in rows}

    def _f(v) -> Optional[float]:
        try:
            x = float(v) if v is not None else None
            if x is None or x <= 0:
                return None
            return x
        except (TypeError, ValueError):
            return None

    out = {}
    for t in tickers:
        r = by_t.get(t)
        if not r:
            continue
        sh = _f(r.get("shares_outstanding"))
        if sh is None:
            mc = _f(r.get("market_cap"))
            px = _f(r.get("last_price"))
            if mc is not None and px is not None:
                sh = mc / px
        if sh is not None:
            out[t] = sh
    return out


def list_subsector_composites(db: Database, market: str | None = None) -> list[dict]:
    """One row per distinct, non-null sub_sector in active securities.
    Each row carries the composite ticker, the human label, the constituent
    count, and a representative parent yf_sector (the modal — sub-sectors
    occasionally span parent sectors via ticker overrides, but the modal
    is the right grouping for browse-mode UI). Scoped to one market when
    `market` is set so the HK browse doesn't show US sub-sectors."""
    with db.get_connection() as conn:
        if market is None:
            rows = conn.execute(
                "SELECT sub_sector, COUNT(*) AS n, "
                "MAX(COALESCE(effective_sector, yf_sector, watchlist_sector)) "
                "  AS parent_sector "
                "FROM securities "
                "WHERE is_active = 1 AND sub_sector IS NOT NULL "
                "AND sub_sector != '' "
                "GROUP BY sub_sector ORDER BY sub_sector"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT sub_sector, COUNT(*) AS n, "
                "MAX(COALESCE(effective_sector, yf_sector, watchlist_sector)) "
                "  AS parent_sector "
                "FROM securities "
                "WHERE is_active = 1 AND market = ? "
                "AND sub_sector IS NOT NULL AND sub_sector != '' "
                "GROUP BY sub_sector ORDER BY sub_sector",
                (market,),
            ).fetchall()
    out = []
    for label, n, parent in rows:
        if n < 2:
            continue
        # Market-namespace the ticker so HK and US composites of the same
        # name (e.g. 'Banks') cache independently in historical_prices.
        out.append({
            "ticker": to_subsector_ticker(label, market=market),
            "sub_sector": label,
            "n_constituents": int(n),
            "parent_sector": parent or "—",
        })
    return out


# ============== Index computation ==============

def compute_subsector_index(sub_sector_label: str,
                              db: Database,
                              base_value: float = 100.0,
                              market: Optional[str] = None) -> list[dict]:
    """Chain-linked Laspeyres index for the supplied sub-sector. Returns
    rows ready for `historical_prices.upsert_rows`. When `market` is set,
    only that market's constituents contribute to the index."""
    tickers = _active_constituents(db, sub_sector_label, market=market)
    if len(tickers) < 2:
        raise ValueError(
            f"sub-sector '{sub_sector_label}' has <2 active constituents")

    shares = _latest_shares_outstanding(db, tickers)
    valid = [t for t in tickers if shares.get(t, 0) > 0]
    if len(valid) < 2:
        raise ValueError(
            f"sub-sector '{sub_sector_label}' has <2 constituents with "
            "shares_outstanding data")

    # ONE bulk read of every constituent series — single Supabase
    # round-trip instead of 30+ (each cache-aside `latest_date` + full
    # series fetch was its own RTT pair). Cuts sub-sector rebuild from
    # ~30s on a 30-ticker bucket down to ~3-5s.
    #
    # Newly-added constituents that haven't been backfilled yet are
    # simply skipped — they'll appear in the index after the next daily
    # price cron + sub-sector cron rebuild. Acceptable since composites
    # rebuild daily.
    from storage.factory import get_prices_repo
    repo = get_prices_repo(db)
    bulk = repo.bulk_get_price_series(valid, "1900-01-01", "2999-12-31")

    series_by_ticker = {}
    for t in valid:
        rows = bulk.get(t) or []
        if not rows:
            continue
        s = pd.Series(
            [float(r["adj_close"]) for r in rows
             if r.get("adj_close") is not None],
            index=pd.to_datetime(
                [r["date"] for r in rows
                 if r.get("adj_close") is not None]),
        )
        s = s[~s.index.duplicated(keep="last")].sort_index()
        if not s.empty:
            series_by_ticker[t] = s
    if len(series_by_ticker) < 2:
        raise ValueError(
            f"sub-sector '{sub_sector_label}' has <2 constituents with "
            "any price history")

    # Outer-join so the index spans every date any constituent traded.
    prices = pd.concat(series_by_ticker, axis=1, join="outer").sort_index()
    ordered_tickers = list(prices.columns)
    shares_vec = [shares[t] for t in ordered_tickers]

    # mcap per (date, ticker)
    mcap = prices.mul(shares_vec, axis=1)

    # "Common with previous" mask — traded on BOTH t-1 and t. The
    # chain-link trick: a new constituent doesn't affect its first day's
    # return because it's not in `common_prev`. From day 2 onwards its
    # mcap participates normally.
    active = mcap.notna()
    common_prev = active & active.shift(1).fillna(False)

    # Per-row sum restricted to common_prev set
    numer = mcap.where(common_prev).sum(axis=1, min_count=1)
    denom = mcap.shift(1).where(common_prev).sum(axis=1, min_count=1)
    raw_ret = (numer / denom) - 1.0
    raw_ret = raw_ret.where(denom > 0).fillna(0.0)

    # Start the index from the first date with ≥2 constituents trading.
    n_active = active.sum(axis=1)
    eligible_mask = n_active >= 2
    if not eligible_mask.any():
        raise ValueError(
            f"sub-sector '{sub_sector_label}' has no date with ≥2 "
            "trading constituents")
    first_eligible = eligible_mask.idxmax()
    ret = raw_ret.loc[first_eligible:].copy()
    ret.iloc[0] = 0.0   # base date — no return

    index = (1.0 + ret).cumprod() * float(base_value)
    return _series_to_price_rows(index)


# ============== Materialise into historical_prices ==============

def rebuild_and_upsert_subsector(sub_sector_label: str,
                                    db: Database,
                                    market: Optional[str] = None) -> dict:
    """Recompute one composite index and upsert it. When `market` is set,
    builds the market-namespaced composite (`&HK:BANKS` / `&US:BANKS`)
    from only that market's constituents. Returns `{ticker, rows, errors}`.
    Errors are caught + surfaced so the `rebuild_all_subsectors` loop
    survives a single bad composite."""
    ticker = to_subsector_ticker(sub_sector_label, market=market)
    out = {"ticker": ticker, "sub_sector": sub_sector_label,
            "rows": 0, "errors": []}
    try:
        rows = compute_subsector_index(sub_sector_label, db, market=market)
        if not rows:
            out["errors"].append("compute produced empty series")
            return out
        repo = get_prices_repo(db)
        repo.upsert_rows(ticker, rows)
        out["rows"] = len(rows)
        log.info("Rebuilt %s (%d rows)", ticker, len(rows))
    except Exception as e:  # noqa: BLE001 — per-composite degrade
        out["errors"].append(f"{type(e).__name__}: {e}")
        log.warning("subsector rebuild failed for %s: %s",
                     sub_sector_label, e)
    return out


def rebuild_all_subsectors(db: Database, market: Optional[str] = None) -> dict:
    """Iterate every active distinct sub_sector and rebuild each composite.
    Used by the daily cron and the `composites rebuild` CLI. When `market`
    is set, only that market's composites get rebuilt — `&HK:` or `&US:`."""
    composites = list_subsector_composites(db, market=market)
    summary = {
        "n_attempted": len(composites),
        "n_succeeded": 0,
        "total_rows_written": 0,
        "errors": [],
        "per_composite": [],
        "elapsed_sec": 0.0,
    }
    t0 = time.time()
    for c in composites:
        r = rebuild_and_upsert_subsector(c["sub_sector"], db, market=market)
        summary["per_composite"].append(r)
        if r["rows"] > 0 and not r["errors"]:
            summary["n_succeeded"] += 1
            summary["total_rows_written"] += r["rows"]
        if r["errors"]:
            summary["errors"].append(
                {"sub_sector": c["sub_sector"], "errors": r["errors"]})
    summary["elapsed_sec"] = round(time.time() - t0, 1)
    log.info("Sub-sector composite rebuild: %d/%d succeeded, %d rows in %.1fs",
              summary["n_succeeded"], summary["n_attempted"],
              summary["total_rows_written"], summary["elapsed_sec"])
    return summary


def label_for_ticker(ticker: str, db: Database) -> Optional[str]:
    """Reverse-lookup: given `&NAME`, return the original sub_sector label
    by enumerating all distinct sub_sectors and re-normalising each."""
    slug = parse_subsector_ticker(ticker)
    if slug is None:
        return None
    for c in list_subsector_composites(db):
        if normalise_subsector_name(c["sub_sector"]) == slug:
            return c["sub_sector"]
    return None
