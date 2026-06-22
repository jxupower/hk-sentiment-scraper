"""Historical US fundamentals — hybrid akshare → yfinance fallback.

Mirrors the HK akshare scraper module shape so callers swap transparently.
Per ticker:

1. **Phase 1 — akshare** (`stock_financial_us_analysis_indicator_em`). Fast
   (~860ms/call), deep history (~20y avg, up to 35y on some names). Covers
   ~80% of the US universe but **misses the entire Financials sector**
   (JPM, BAC, WFC, GS, MS, C, BLK all return None) plus a few odd share
   classes (BF-B → akshare wants BF_B; verified working).

2. **Phase 2 — yfinance fallback** (`Ticker.income_stmt` + `.balance_sheet`
   + `.cashflow`, annual). Slower (~1s/call), shallower (~5y), but
   complete coverage of the remaining tickers.

Writes to `fundamentals_snapshots` with `source='akshare_annual'` for
phase 1 rows and `source='yfinance_annual'` for phase 2 rows, so
downstream consumers (FactorScoringEngine, Stock Research CAGR helpers,
Backtest engine) treat the historical depth uniformly while the `source`
column lets us monitor coverage drift over time.

Ratio conventions match the HK scraper: store as FRACTIONS in the schema
(0.254 = 25.4%) even though akshare returns PERCENTS — divide by 100 on
ingest.
"""
from __future__ import annotations

import math
import time
from typing import Optional

import akshare as ak
import yfinance as yf

from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# akshare phase: column mapping + per-row converter
# ---------------------------------------------------------------------------

# Map akshare US wide-format columns → our fundamentals_snapshots column
# names. Field names differ slightly from the HK endpoint (US uses
# PARENT_HOLDER_NETPROFIT vs HK's HOLDER_PROFIT; US uses ROE_AVG vs HK's
# ROE_YEARLY). Verified empirically against AAPL FY2025 response.
_AK_US_RATIO_MAP = {
    # akshare column                       → our column,           needs /100?
    "ROE_AVG":                              ("return_on_equity",        True),  # %
    "ROA":                                  ("return_on_assets",        True),  # %
    "OPERATE_INCOME_YOY":                   ("revenue_growth",          True),  # %
    "PARENT_HOLDER_NETPROFIT_YOY":          ("earnings_growth",         True),  # %
    "NET_PROFIT_RATIO":                     ("profit_margins",          True),  # %
    "GROSS_PROFIT_RATIO":                   ("operating_margins",       True),  # gross-margin proxy (same caveat as HK scraper)
    "CURRENT_RATIO":                        ("current_ratio",           False), # ratio
    "DEBT_ASSET_RATIO":                     ("debt_to_equity",          True),  # debt/asset %; stored in debt_to_equity slot (caveat: different metric)
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


def _ticker_to_akshare_us(ticker: str) -> str:
    """yfinance form → akshare US form. Share classes use underscore
    (`BRK-B` → `BRK_B`); other tickers pass through uppercase."""
    if not ticker:
        return ticker
    return ticker.strip().upper().replace("-", "_")


def _akshare_row_to_snapshot(ak_row: dict) -> dict:
    """Convert one akshare wide-format row to a fundamentals_snapshots dict.
    Mirrors the HK scraper's `_row_to_snapshot` but uses the US-endpoint
    column names. `trailing_pe`/`price_to_book`/`market_cap` left NULL —
    they require a spot price computed at backtest time."""
    snapshot: dict = {}
    non_null_count = 0
    for ak_col, (our_col, needs_pct) in _AK_US_RATIO_MAP.items():
        raw = _coerce_finite(ak_row.get(ak_col))
        if raw is None:
            snapshot[our_col] = None
            continue
        snapshot[our_col] = raw / 100.0 if needs_pct else raw
        non_null_count += 1

    snapshot["trailing_pe"]      = None  # computed at backtest time
    snapshot["forward_pe"]       = None
    snapshot["price_to_book"]    = None  # computed at backtest time
    snapshot["ev_to_ebitda"]     = None
    snapshot["dividend_yield"]   = None
    snapshot["market_cap"]       = None  # price × shares_outstanding at backtest
    snapshot["beta"]             = None
    snapshot["last_price"]       = None
    snapshot["currency"]         = ak_row.get("CURRENCY_ABBR") or "USD"
    snapshot["free_cashflow"]    = None  # not on akshare US analysis endpoint
    snapshot["operating_margins"] = None  # overwrite the gross-margin proxy

    # Per-share metrics: BASIC_EPS direct; shares_outstanding derived from
    # PARENT_HOLDER_NETPROFIT / BASIC_EPS as in HK. BPS not on this endpoint.
    eps_basic = _coerce_finite(ak_row.get("BASIC_EPS"))
    snapshot["eps_ttm"] = eps_basic
    snapshot["bps"]     = None
    net_income = _coerce_finite(ak_row.get("PARENT_HOLDER_NETPROFIT"))
    if net_income is not None and eps_basic is not None and eps_basic != 0:
        snapshot["shares_outstanding"] = net_income / eps_basic
    else:
        snapshot["shares_outstanding"] = None

    snapshot["data_completeness"] = round(non_null_count / len(_AK_US_RATIO_MAP), 3)
    return snapshot


def _fetch_via_akshare(ticker: str) -> list[tuple[str, dict]]:
    """Try akshare US endpoint. Returns [] on failure (caller falls back)."""
    ak_sym = _ticker_to_akshare_us(ticker)
    try:
        df = ak.stock_financial_us_analysis_indicator_em(
            symbol=ak_sym, indicator="年报",
        )
    except Exception:
        # akshare raises TypeError on missing tickers (NoneType subscript) —
        # silent return so the caller falls through to yfinance.
        return []
    if df is None or df.empty:
        return []

    out: list[tuple[str, dict]] = []
    for record in df.to_dict("records"):
        report_date_raw = record.get("REPORT_DATE")
        if not report_date_raw:
            continue
        snapshot_date = str(report_date_raw)[:10]
        snapshot = _akshare_row_to_snapshot(record)
        out.append((snapshot_date, snapshot))
    return out


# ---------------------------------------------------------------------------
# yfinance phase: pull annual statements + derive ratios
# ---------------------------------------------------------------------------

# yfinance line-item names can vary by ticker; we probe a list of synonyms
# and take the first hit. These cover ~95% of US tickers in the Russell
# 3000.
_LINE_REVENUE     = ["Total Revenue", "Operating Revenue", "Revenue"]
_LINE_GROSS_PROF  = ["Gross Profit"]
_LINE_NET_INCOME  = ["Net Income", "Net Income Common Stockholders",
                      "Net Income From Continuing Operations"]
_LINE_EQUITY      = ["Stockholders Equity", "Total Equity Gross Minority Interest",
                      "Common Stock Equity"]
_LINE_TOT_ASSETS  = ["Total Assets"]
_LINE_CURR_ASSETS = ["Current Assets"]
_LINE_CURR_LIAB   = ["Current Liabilities"]
_LINE_TOT_DEBT    = ["Total Debt", "Long Term Debt And Capital Lease Obligation",
                      "Long Term Debt"]
_LINE_FCF         = ["Free Cash Flow"]


def _pick_line(stmt_df, candidates: list[str], col) -> Optional[float]:
    """Return the first non-null line-item value for one statement column.
    Defensive against `col` missing from this particular statement (balance
    sheet + cashflow can have a different period set than the income
    statement — yfinance silently returns sparse DataFrames in that case)."""
    if stmt_df is None or stmt_df.empty:
        return None
    if col not in stmt_df.columns:
        return None
    for name in candidates:
        if name not in stmt_df.index:
            continue
        try:
            v = stmt_df.at[name, col]
        except (KeyError, IndexError):
            continue
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                continue
            return f
        except (TypeError, ValueError):
            continue
    return None


def _fetch_via_yfinance(ticker: str) -> list[tuple[str, dict]]:
    """Fallback path. Pulls 3 annual statements via yfinance and derives the
    same ratio dict shape as the akshare path. Returns [] on total failure.
    Defensive against yfinance's habit of returning sparse DataFrames where
    income / balance / cashflow have different period sets — every per-row
    extraction goes through `_pick_line` which tolerates missing columns."""
    try:
        t = yf.Ticker(ticker)
        income = t.income_stmt          # annual
        balance = t.balance_sheet
        cashflow = t.cashflow
        info = t.info or {}
    except Exception as e:
        logger.warning("yfinance fundamentals fetch failed [%s]: %s", ticker, e)
        return []

    if income is None or income.empty:
        return []

    try:
        return _build_yfinance_history(ticker, income, balance, cashflow, info)
    except Exception as e:
        # Defensive — any unexpected pandas / yfinance quirk drops the
        # ticker rather than killing the seed.
        logger.warning("yfinance history build failed [%s]: %s", ticker, e)
        return []


def _build_yfinance_history(ticker, income, balance, cashflow, info) -> list[tuple[str, dict]]:

    # yfinance returns columns = period-end dates (Timestamp), newest first.
    # Walk them oldest→newest so YoY uses the prior column.
    cols = sorted(income.columns)
    out: list[tuple[str, dict]] = []
    prev_revenue: Optional[float] = None
    prev_net_income: Optional[float] = None

    eps_ttm_info = _coerce_finite(info.get("trailingEps"))
    shares_outstanding_info = _coerce_finite(info.get("sharesOutstanding"))

    for col in cols:
        snapshot_date = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]

        # Income statement
        revenue     = _pick_line(income,   _LINE_REVENUE,    col)
        gross_prof  = _pick_line(income,   _LINE_GROSS_PROF, col)
        net_income  = _pick_line(income,   _LINE_NET_INCOME, col)
        # Balance sheet
        equity      = _pick_line(balance,  _LINE_EQUITY,     col)
        tot_assets  = _pick_line(balance,  _LINE_TOT_ASSETS, col)
        curr_assets = _pick_line(balance,  _LINE_CURR_ASSETS, col)
        curr_liab   = _pick_line(balance,  _LINE_CURR_LIAB,  col)
        tot_debt    = _pick_line(balance,  _LINE_TOT_DEBT,   col)
        # Cash flow
        fcf         = _pick_line(cashflow, _LINE_FCF,        col)

        roe = (net_income / equity) if (net_income is not None
                                         and equity is not None
                                         and equity != 0) else None
        roa = (net_income / tot_assets) if (net_income is not None
                                              and tot_assets is not None
                                              and tot_assets != 0) else None
        gross_margin = (gross_prof / revenue) if (gross_prof is not None
                                                    and revenue is not None
                                                    and revenue != 0) else None
        net_margin = (net_income / revenue) if (net_income is not None
                                                  and revenue is not None
                                                  and revenue != 0) else None
        de = (tot_debt / equity) if (tot_debt is not None
                                       and equity is not None
                                       and equity != 0) else None
        curr_ratio = (curr_assets / curr_liab) if (curr_assets is not None
                                                     and curr_liab is not None
                                                     and curr_liab != 0) else None
        rev_yoy = ((revenue - prev_revenue) / prev_revenue
                    if (revenue is not None and prev_revenue is not None
                         and prev_revenue != 0) else None)
        earn_yoy = ((net_income - prev_net_income) / prev_net_income
                     if (net_income is not None and prev_net_income is not None
                          and prev_net_income != 0) else None)

        # bps computed per period using equity + the single shares figure
        # from .info (a TTM proxy; ideal would be per-period shares but
        # yfinance doesn't expose annual history of shares outstanding
        # reliably). Acceptable for screening; backtest will mostly use
        # the akshare-tier rows anyway.
        bps = (equity / shares_outstanding_info
                if (equity is not None
                     and shares_outstanding_info
                     and shares_outstanding_info > 0)
                else None)

        non_null = sum(1 for v in (roe, roa, gross_margin, net_margin, de,
                                     curr_ratio, rev_yoy, earn_yoy) if v is not None)
        snapshot = {
            "return_on_equity":   roe,
            "return_on_assets":   roa,
            "revenue_growth":     rev_yoy,
            "earnings_growth":    earn_yoy,
            "profit_margins":     net_margin,
            "operating_margins":  None,   # gross-margin proxy lives in `operating_margins` slot in HK; keep NULL here to be explicit
            "current_ratio":      curr_ratio,
            "debt_to_equity":     de,
            "trailing_pe":        None,
            "forward_pe":         None,
            "price_to_book":      None,
            "ev_to_ebitda":       None,
            "dividend_yield":     None,
            "market_cap":         None,
            "beta":               None,
            "last_price":         None,
            "currency":           "USD",
            "free_cashflow":      fcf,
            # Per-share fields. eps_ttm comes from .info (one value applies
            # to the most recent period; older periods get None — acceptable
            # since backtest computes EPS-from-prices on the akshare path).
            "eps_ttm":            eps_ttm_info if col == cols[-1] else None,
            "bps":                bps,
            "shares_outstanding": shares_outstanding_info if col == cols[-1] else None,
            "data_completeness":  round(non_null / 8, 3),
        }
        out.append((snapshot_date, snapshot))

        prev_revenue = revenue
        prev_net_income = net_income

    return out


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def fetch_history(ticker: str) -> tuple[str, list[tuple[str, dict]]]:
    """Pull all available annual fundamentals for one US ticker.

    Returns `(source_used, [(snapshot_date, snapshot_dict), …])` so the
    caller can write the right `source` value per row. `source_used` is
    one of: 'akshare_annual', 'yfinance_annual', '' (no data).
    """
    history = _fetch_via_akshare(ticker)
    if history:
        return "akshare_annual", history
    history = _fetch_via_yfinance(ticker)
    if history:
        return "yfinance_annual", history
    return "", []


def fetch_many(tickers: list[str], fundamentals_repo,
               throttle_seconds: float = 0.5) -> dict:
    """Loop over US tickers; write each ticker's history to the repo.

    Returns summary dict: `{attempted, snapshots_written, akshare_hit,
    yfinance_fallback, no_data_tickers, failed_tickers}`. Logged per-10
    so long seeds remain observable.
    """
    attempted = 0
    written_snapshots = 0
    akshare_hit = 0
    yfinance_fallback = 0
    no_data_tickers = 0
    failed_tickers = 0

    for i, ticker in enumerate(tickers, start=1):
        attempted += 1
        try:
            source, history = fetch_history(ticker)
        except Exception as e:
            logger.warning("Unexpected error for %s: %s", ticker, e)
            failed_tickers += 1
            time.sleep(throttle_seconds)
            continue

        if not history:
            no_data_tickers += 1
            time.sleep(throttle_seconds)
            continue

        if source == "akshare_annual":
            akshare_hit += 1
        elif source == "yfinance_annual":
            yfinance_fallback += 1

        for snapshot_date, snapshot in history:
            try:
                # FundamentalsRepository.upsert_snapshot infers market from
                # ticker via utils.market.market_of_ticker. The cloud repo
                # accepts a `source` kwarg; the local repo signature does
                # not — call dynamically.
                if hasattr(fundamentals_repo, "upsert_snapshot"):
                    try:
                        fundamentals_repo.upsert_snapshot(
                            ticker, snapshot_date, snapshot, source=source,
                        )
                    except TypeError:
                        # SQLite repo: no `source` kwarg
                        fundamentals_repo.upsert_snapshot(
                            ticker, snapshot_date, snapshot,
                        )
                    written_snapshots += 1
            except Exception as e:
                logger.warning("upsert failed [%s %s]: %s",
                                ticker, snapshot_date, e)

        if i % 10 == 0:
            logger.info(
                "us-fundamentals progress: %d/%d "
                "(snapshots=%d, akshare=%d, yf=%d, no_data=%d, failed=%d)",
                i, len(tickers), written_snapshots, akshare_hit,
                yfinance_fallback, no_data_tickers, failed_tickers,
            )
        time.sleep(throttle_seconds)

    summary = {
        "attempted": attempted,
        "snapshots_written": written_snapshots,
        "akshare_hit": akshare_hit,
        "yfinance_fallback": yfinance_fallback,
        "no_data_tickers": no_data_tickers,
        "failed_tickers": failed_tickers,
    }
    logger.info("us-fundamentals seed complete: %s", summary)
    return summary
