"""Historical HK fundamentals via akshare (open-source, free).

akshare's `stock_financial_hk_analysis_indicator_em` returns ~9 years of annual
ratios per HK ticker in wide format. We map its columns to our
`fundamentals_snapshots` schema so the same downstream code (FactorScoringEngine,
screens, backtest) can consume historical snapshots without knowing where they
came from.

Important data caveat (documented in plan + dashboard caveat banner):
- akshare returns ANNUAL data only (HK companies report semi-annually, but
  the akshare endpoint flattens). 9 data points per ticker is thin.
- Data is as-restated, not as-originally-reported (no point-in-time guarantee).
  Backtest engine applies a 60-day reporting lag as a conservative mitigation.
- ROE / ROA / margins arrive as PERCENT values from akshare (e.g. 25.4 = 25.4%);
  the EXTENDED_FIELDS in fundamentals_snapshots store them as FRACTIONS
  (0.254 = 25.4%) to match yfinance convention. We divide by 100 on ingest.
- HK ticker format conversion: "0700.HK" → "00700" (akshare uses 5-digit
  zero-padded without the .HK suffix).
"""
import math
import time
from typing import Optional

import akshare as ak

from utils.logger import get_logger

logger = get_logger(__name__)


# Map akshare wide-format columns → our fundamentals_snapshots column names.
# All ratios are stored as FRACTIONS in our schema (consistent with yfinance .info convention).
# akshare returns PERCENTS for most ratio fields → we divide by 100 on ingest.
RATIO_MAP = {
    # akshare column                   → our column,             needs /100?
    "ROE_YEARLY":                      ("return_on_equity",        True),   # %
    "ROA":                             ("return_on_assets",        True),   # %
    "OPERATE_INCOME_YOY":              ("revenue_growth",          True),   # %
    "HOLDER_PROFIT_YOY":               ("earnings_growth",         True),   # %
    "NET_PROFIT_RATIO":                ("profit_margins",          True),   # %
    "GROSS_PROFIT_RATIO":              ("operating_margins",       True),   # akshare doesn't expose operating margin per se; gross is the closest proxy
    "CURRENT_RATIO":                   ("current_ratio",           False),  # ratio
    "DEBT_ASSET_RATIO":                ("debt_to_equity",          True),   # akshare gives debt/asset %; we store under debt_to_equity (different metric but in same column slot — caveat)
}


def _coerce_finite(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _ticker_to_akshare(ticker: str) -> str:
    """0700.HK → 00700  (akshare uses 5-digit zero-padded, no .HK suffix)."""
    code = ticker.split(".")[0]
    try:
        return f"{int(code):05d}"
    except (ValueError, TypeError):
        return code


def _row_to_snapshot(ak_row: dict, eps_basic: Optional[float],
                     bps: Optional[float]) -> dict:
    """Convert one akshare wide-format row to a fundamentals_snapshots dict.

    Note: trailing_pe and price_to_book are LEFT NULL here — they require a
    spot price to compute, which we don't have at scrape time. The backtest
    engine joins to historical_prices to compute these on-the-fly during the
    as-of query.
    """
    snapshot = {}
    non_null_count = 0
    for ak_col, (our_col, needs_pct) in RATIO_MAP.items():
        raw = _coerce_finite(ak_row.get(ak_col))
        if raw is None:
            snapshot[our_col] = None
            continue
        snapshot[our_col] = raw / 100.0 if needs_pct else raw
        non_null_count += 1

    # Columns we explicitly cannot fill from akshare's analysis endpoint
    snapshot["trailing_pe"]   = None     # computed at backtest time using price + eps_ttm
    snapshot["forward_pe"]    = None     # not available historically
    snapshot["price_to_book"] = None     # computed at backtest time using price + bps
    snapshot["ev_to_ebitda"]  = None
    snapshot["dividend_yield"] = None
    snapshot["market_cap"]    = None     # computed at backtest time using price × shares_outstanding
    snapshot["beta"]          = None
    snapshot["last_price"]    = None
    snapshot["currency"]      = ak_row.get("CURRENCY") or "HKD"
    snapshot["operating_margins"] = None  # overwrite incorrect mapping; we don't have true op margin
    snapshot["free_cashflow"] = None      # PER_NETCASH_OPERATE is per-share, not total — leave NULL

    # Per-share metrics for as-of P/E and P/B computation at backtest time
    snapshot["eps_ttm"] = eps_basic       # use BASIC_EPS as a TTM proxy for annual data
    snapshot["bps"]     = bps
    # Derive shares outstanding from net income / EPS (approximate; akshare
    # doesn't expose shares directly on the analysis endpoint).
    net_income = _coerce_finite(ak_row.get("HOLDER_PROFIT"))
    if net_income is not None and eps_basic is not None and eps_basic != 0:
        snapshot["shares_outstanding"] = net_income / eps_basic
    else:
        snapshot["shares_outstanding"] = None

    snapshot["data_completeness"] = round(non_null_count / len(RATIO_MAP), 3)
    return snapshot


def fetch_history(ticker: str) -> list[tuple[str, dict]]:
    """Pull all available annual fundamentals history for one HK ticker.

    Returns list of (snapshot_date, snapshot_dict) tuples, suitable for
    upserting via FundamentalsRepository.upsert_snapshot.
    Returns [] on any failure (akshare can be flaky).
    """
    ak_code = _ticker_to_akshare(ticker)
    try:
        df = ak.stock_financial_hk_analysis_indicator_em(symbol=ak_code, indicator="年度")
    except Exception as e:
        logger.warning("akshare fetch failed [%s -> %s]: %s", ticker, ak_code, e)
        return []

    if df is None or df.empty:
        return []

    out: list[tuple[str, dict]] = []
    for record in df.to_dict("records"):
        # REPORT_DATE format: "2023-12-31 00:00:00"
        report_date_raw = record.get("REPORT_DATE")
        if not report_date_raw:
            continue
        snapshot_date = str(report_date_raw)[:10]  # YYYY-MM-DD

        eps_basic = _coerce_finite(record.get("BASIC_EPS"))
        bps       = _coerce_finite(record.get("BPS"))
        snapshot  = _row_to_snapshot(record, eps_basic, bps)
        out.append((snapshot_date, snapshot))

    return out


def fetch_many(tickers: list[str], fundamentals_repo,
               securities_repo, throttle_seconds: float = 0.5) -> dict:
    """Loop over tickers, write each ticker's history to the repo.

    Returns summary dict: {attempted, written, no_data, failed}.
    Each successful ticker contributes ~9 historical snapshots.
    """
    attempted = 0
    written_snapshots = 0
    no_data_tickers = 0
    failed_tickers = 0

    for i, ticker in enumerate(tickers, start=1):
        attempted += 1
        try:
            history = fetch_history(ticker)
        except Exception as e:
            logger.warning("Unexpected error for %s: %s", ticker, e)
            failed_tickers += 1
            time.sleep(throttle_seconds)
            continue

        if not history:
            no_data_tickers += 1
            time.sleep(throttle_seconds)
            continue

        for snapshot_date, snapshot in history:
            try:
                fundamentals_repo.upsert_snapshot(ticker, snapshot_date, snapshot)
                written_snapshots += 1
            except Exception as e:
                logger.warning("upsert failed [%s %s]: %s", ticker, snapshot_date, e)

        if i % 10 == 0:
            logger.info("akshare progress: %d/%d (snapshots=%d, no_data=%d, failed=%d)",
                        i, len(tickers), written_snapshots, no_data_tickers, failed_tickers)
        time.sleep(throttle_seconds)

    summary = {
        "attempted": attempted,
        "snapshots_written": written_snapshots,
        "no_data_tickers": no_data_tickers,
        "failed_tickers": failed_tickers,
    }
    logger.info("akshare seed complete: %s", summary)
    return summary
