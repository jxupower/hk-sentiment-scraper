"""Forensic analysis — heuristic red-flag detector.

Plain Bagel's research process emphasizes a "forensic" pass to spot:
  - Adjusted-vs-reported figure gaps (companies often inflate "adjusted earnings")
  - Related-party transactions (we can't detect these from akshare/yfinance)
  - Management compensation outliers (also not available in our data)
  - Earnings vs cash divergence (accruals quality)
  - Share dilution patterns
  - Sudden debt increases

We CAN detect heuristically from the per-ticker history in fundamentals_snapshots:
  1. Earnings/cash divergence — gap between reported earnings and operating cash
  2. Persistent share dilution (>5%/y for 2+ years)
  3. Debt explosion (debt_to_equity > 2× the 3y trailing average)
  4. Margin compression (profit_margins down >30% YoY)
  5. Revenue/earnings divergence (revenue up but earnings down 2+ consecutive years)

We CAN'T detect from this data:
  - Related-party transactions
  - True non-GAAP "adjusted" vs reported gap (akshare gives one number)
  - Management compensation
  - Auditor changes

For #1 (earnings vs cash), our proxy is whether margins are degrading while
revenue grows — a classic "channel stuffing" signal.

Each RedFlag has severity (high/medium/low) and a one-line explanation.
"""
import math
import sqlite3
import statistics
from dataclasses import dataclass
from typing import Optional


@dataclass
class RedFlag:
    id: str            # short identifier for the check
    severity: str      # "high" | "medium" | "low"
    title: str         # short user-visible label
    detail: str        # one-line explanation with the numbers involved


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


def detect_red_flags(ticker: str, db_path: str) -> list[RedFlag]:
    """Run all heuristic forensic checks for a ticker. Returns ordered list
    of RedFlags, most severe first."""
    history = _load_history(ticker, db_path)
    if not history or len(history) < 3:
        return []

    flags: list[RedFlag] = []
    flags.extend(_check_share_dilution(history))
    flags.extend(_check_debt_explosion(history))
    flags.extend(_check_margin_compression(history))
    flags.extend(_check_revenue_earnings_divergence(history))
    flags.extend(_check_negative_earnings_trajectory(history))

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: severity_rank.get(f.severity, 99))
    return flags


def _load_history(ticker: str, db_path: str) -> list[dict]:
    """Load all fundamentals snapshots for ticker, sorted oldest first."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT snapshot_date, shares_outstanding, debt_to_equity,
                   profit_margins, revenue_growth, earnings_growth,
                   eps_ttm, return_on_equity
            FROM fundamentals_snapshots
            WHERE ticker = ?
            ORDER BY snapshot_date ASC
        """, (ticker,)).fetchall()
        return [dict(r) for r in rows]


def _check_share_dilution(history: list[dict]) -> list[RedFlag]:
    """Persistent share count growth signals dilutive equity issuance."""
    out: list[RedFlag] = []
    shares = [(r["snapshot_date"], _finite(r["shares_outstanding"])) for r in history]
    shares = [(d, s) for d, s in shares if s is not None]
    if len(shares) < 4:
        return out

    # Compute YoY growth in shares; flag if 2+ consecutive years > 5%
    yoy_growth = []
    for i in range(1, len(shares)):
        prev_s = shares[i - 1][1]
        cur_s = shares[i][1]
        if prev_s > 0:
            yoy_growth.append((shares[i][0], (cur_s / prev_s) - 1))
    consecutive = 0
    max_consec = 0
    for _, g in yoy_growth:
        if g > 0.05:
            consecutive += 1
            max_consec = max(max_consec, consecutive)
        else:
            consecutive = 0
    if max_consec >= 2:
        latest_growth = yoy_growth[-1][1] if yoy_growth else 0
        out.append(RedFlag(
            id="dilution",
            severity="medium" if max_consec < 4 else "high",
            title="Persistent share dilution",
            detail=f"Share count grew >5% YoY for {max_consec} consecutive years "
                   f"(latest YoY: +{latest_growth*100:.1f}%). "
                   "May indicate equity-funded growth or stock-based compensation.",
        ))
    return out


def _check_debt_explosion(history: list[dict]) -> list[RedFlag]:
    """D/E jumping >2× its trailing 3y average signals a sudden leverage change."""
    out: list[RedFlag] = []
    de_series = [(r["snapshot_date"], _finite(r["debt_to_equity"])) for r in history]
    de_series = [(d, v) for d, v in de_series if v is not None]
    if len(de_series) < 4:
        return out

    latest_date, latest_de = de_series[-1]
    # Trailing 3y average (exclude latest itself)
    prev_3 = [v for _, v in de_series[-4:-1]]
    if len(prev_3) >= 2:
        avg_prev = sum(prev_3) / len(prev_3)
        if avg_prev > 0 and latest_de > 2 * avg_prev and latest_de > 50:
            out.append(RedFlag(
                id="debt_explosion",
                severity="high",
                title="Sudden debt increase",
                detail=f"D/E ratio of {latest_de:.0f}% is {latest_de/avg_prev:.1f}× "
                       f"the prior 3-year average of {avg_prev:.0f}%. "
                       "Investigate funding strategy and refinancing risk.",
            ))
    return out


def _check_margin_compression(history: list[dict]) -> list[RedFlag]:
    """Profit margin dropping >30% YoY signals operational deterioration."""
    out: list[RedFlag] = []
    margins = [(r["snapshot_date"], _finite(r["profit_margins"])) for r in history]
    margins = [(d, v) for d, v in margins if v is not None]
    if len(margins) < 2:
        return out

    latest_date, latest_m = margins[-1]
    prev_date, prev_m = margins[-2]
    if prev_m is not None and prev_m > 0.05:  # only flag if prev was meaningfully positive
        change = (latest_m - prev_m) / abs(prev_m)
        if change < -0.30:
            out.append(RedFlag(
                id="margin_compression",
                severity="medium",
                title="Margin compression",
                detail=f"Profit margin dropped from {prev_m*100:.1f}% to "
                       f"{latest_m*100:.1f}% YoY ({change*100:+.0f}%). "
                       "Investigate pricing pressure or cost inflation.",
            ))
    return out


def _check_revenue_earnings_divergence(history: list[dict]) -> list[RedFlag]:
    """Revenue growing but earnings shrinking for 2+ consecutive years —
    classic 'growth at the cost of profitability' or accruals build-up."""
    out: list[RedFlag] = []
    rg_series = [(r["snapshot_date"], _finite(r["revenue_growth"])) for r in history]
    eg_series = [(r["snapshot_date"], _finite(r["earnings_growth"])) for r in history]
    rg_series = [(d, v) for d, v in rg_series if v is not None]
    eg_series = [(d, v) for d, v in eg_series if v is not None]
    if len(rg_series) < 3 or len(eg_series) < 3:
        return out

    # Build aligned series of (date, rg, eg) for the same dates
    aligned = []
    eg_by_date = {d: v for d, v in eg_series}
    for d, rg in rg_series:
        eg = eg_by_date.get(d)
        if eg is not None:
            aligned.append((d, rg, eg))
    if len(aligned) < 3:
        return out

    # Count consecutive years where revenue > 0 AND earnings < 0
    last_3 = aligned[-3:]
    divergent_count = sum(1 for _, rg, eg in last_3 if rg > 0.05 and eg < -0.05)
    if divergent_count >= 2:
        out.append(RedFlag(
            id="revenue_earnings_divergence",
            severity="medium",
            title="Revenue up, earnings down",
            detail=f"Revenue grew while earnings shrank in {divergent_count} of last 3 years. "
                   "Possible margin pressure, accruals build-up, or aggressive growth spend.",
        ))
    return out


def _check_negative_earnings_trajectory(history: list[dict]) -> list[RedFlag]:
    """3+ consecutive years of declining earnings is a structural concern."""
    out: list[RedFlag] = []
    eg = [_finite(r["earnings_growth"]) for r in history]
    eg = [v for v in eg if v is not None]
    if len(eg) < 3:
        return out
    last_3 = eg[-3:]
    if all(v < 0 for v in last_3):
        avg = sum(last_3) / 3
        out.append(RedFlag(
            id="declining_earnings",
            severity="high",
            title="Sustained earnings decline",
            detail=f"Earnings declined for 3 consecutive years (avg {avg*100:+.0f}%). "
                   "Could be cyclical bottom OR structural impairment — check the story.",
        ))
    return out
