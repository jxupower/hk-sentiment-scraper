"""Dual-source fetcher for raw financial statements: income statement,
balance sheet, cash flow.

Primary source: yfinance `.income_stmt` / `.balance_sheet` / `.cashflow`
(English line items, standardized labels). Reliable for HK large caps and
most mid-caps, sparse for REITs, SPACs, and some small caps.

Fallback source: akshare `stock_financial_hk_report_em` (Chinese line items,
better HK coverage). Used when yfinance returns empty DataFrames.

Public API:
    fetch_statements(ticker) -> dict[str, list[dict]]

Returned shape (per statement type):
    {
      "income":   [{"period_end_date": "2024-12-31",
                    "period_type": "annual" | "semiannual" | "quarterly",
                    "source": "yfinance" | "akshare",
                    "currency": "HKD" | "CNY" | None,
                    "line_items": {"Total Revenue": 612345.0, ...}}, ...],
      "balance":  [...],
      "cashflow": [...],
    }

The list per statement_type is sorted newest-first.
"""
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Minimum annual periods before we consider yfinance "good enough" and skip
# the semi-annual fill-in fetch. <5 years (recent IPOs) triggers the fill-in.
MIN_ANNUAL_PERIODS = 5


def fetch_statements(ticker: str) -> dict[str, list[dict]]:
    """Fetch income/balance/cashflow for one ticker, oldest available source-wise.
    Returns {} on total failure. Each statement_type may independently be empty."""
    out = {"income": [], "balance": [], "cashflow": []}

    yf_rows = _fetch_yfinance(ticker)
    for stype in ("income", "balance", "cashflow"):
        out[stype].extend(yf_rows.get(stype, []))

    # If yfinance gave us <5 annual periods for any statement, try the quarterly
    # endpoint as fill-in (for HK, quarterly is usually semi-annual)
    needs_fillin = any(
        sum(1 for r in out[stype] if r["period_type"] == "annual") < MIN_ANNUAL_PERIODS
        for stype in ("income", "balance", "cashflow")
    )
    if needs_fillin:
        for stype, rows in _fetch_yfinance(ticker, quarterly=True).items():
            out[stype].extend(rows)

    # If any statement is still empty, hit akshare as fallback
    if any(not out[stype] for stype in ("income", "balance", "cashflow")):
        ak_rows = _fetch_akshare(ticker)
        for stype in ("income", "balance", "cashflow"):
            if not out[stype]:
                out[stype] = ak_rows.get(stype, [])

    # Deduplicate by period_end_date keeping the first-seen, which is the
    # annual row (annual fetch runs before the quarterly fill-in). yfinance
    # sometimes returns the same date in both the annual and quarterly endpoint
    # (the H2 close coincides with the fiscal-year close); we want one entry.
    for stype in out:
        seen = set()
        deduped = []
        for r in out[stype]:
            if r["period_end_date"] in seen:
                continue
            seen.add(r["period_end_date"])
            deduped.append(r)
        deduped.sort(key=lambda r: r["period_end_date"], reverse=True)
        out[stype] = deduped

    return out


# ============== yfinance ==============

_YFINANCE_ATTR_MAP = {
    "income":   ("income_stmt", "quarterly_income_stmt"),
    "balance":  ("balance_sheet", "quarterly_balance_sheet"),
    "cashflow": ("cashflow", "quarterly_cashflow"),
}


def _fetch_yfinance(ticker: str, *, quarterly: bool = False) -> dict[str, list[dict]]:
    """Pull all three statements from yfinance. quarterly=True uses the quarterly
    endpoints (which for HK companies typically return semi-annual data)."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not available")
        return {}

    out: dict[str, list[dict]] = {"income": [], "balance": [], "cashflow": []}
    try:
        t = yf.Ticker(ticker)
        currency = (t.info.get("financialCurrency") or t.info.get("currency"))
    except Exception as e:
        logger.warning("yfinance Ticker(%s) failed: %s", ticker, e)
        return out

    period_type = "quarterly" if quarterly else "annual"
    for stype, (annual_attr, quarterly_attr) in _YFINANCE_ATTR_MAP.items():
        attr = quarterly_attr if quarterly else annual_attr
        try:
            df = getattr(t, attr, None)
        except Exception as e:
            logger.warning("yfinance %s.%s failed: %s", ticker, attr, e)
            continue
        if df is None or df.empty:
            continue

        # df is line-items as index, period-end dates as columns.
        # Each column is one statement period.
        for col in df.columns:
            period_end = _to_iso_date(col)
            if not period_end:
                continue
            line_items = {}
            for li, v in df[col].items():
                fv = _to_finite(v)
                if fv is not None:
                    line_items[str(li)] = fv
            if not line_items:
                continue
            # For HK companies the quarterly endpoint often gives semi-annual
            # data — relabel if reporting cadence looks semi-annual
            effective_period_type = period_type
            if quarterly and _looks_semiannual([_to_iso_date(c) for c in df.columns]):
                effective_period_type = "semiannual"
            out[stype].append({
                "period_end_date": period_end,
                "period_type": effective_period_type,
                "source": "yfinance",
                "currency": currency,
                "line_items": line_items,
            })

    return out


def _to_iso_date(v) -> Optional[str]:
    """yfinance returns columns as pd.Timestamp; coerce to YYYY-MM-DD."""
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    s = str(v)
    return s[:10] if len(s) >= 10 else None


def _to_finite(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _looks_semiannual(period_ends: list[Optional[str]]) -> bool:
    """If consecutive period-end-dates are ~6 months apart, this is semi-annual
    reporting (typical for HK). True quarterly would be ~3 months apart."""
    dates = sorted([d for d in period_ends if d], reverse=True)
    if len(dates) < 2:
        return False
    from datetime import datetime
    try:
        d0 = datetime.fromisoformat(dates[0])
        d1 = datetime.fromisoformat(dates[1])
    except ValueError:
        return False
    months = abs((d0.year - d1.year) * 12 + (d0.month - d1.month))
    return 5 <= months <= 7


# ============== akshare ==============

# Chinese -> English mapping for the most common line items. Covers ~30 items
# spanning income / balance / cashflow. Untranslated items are passed through
# with their Chinese key so the UI can render them (with a faint warning badge).
# Hand-curated translations for top common Chinese line items returned by
# akshare's stock_financial_hk_report_em. Untranslated items pass through with
# their Chinese key + a faint visual marker in the UI table.
AKSHARE_LINE_ITEM_MAP = {
    # Income statement (利润表 / 损益表)
    "营业额":             "Total Revenue",
    "其他营业收入":       "Other Operating Revenue",
    "营运收入":           "Operating Revenue",
    "营运支出":           "Operating Expenses",
    "营业成本":           "Cost Of Revenue",
    "毛利":               "Gross Profit",
    "其他收益":           "Other Income",
    "销售及分销费用":     "Selling And Distribution Expenses",
    "行政开支":           "Administrative Expenses",
    "营业利润":           "Operating Income",
    "财务费用":           "Interest Expense",
    "联营公司及合营公司":  "Equity Method Investments",
    "除税前溢利":         "Pretax Income",
    "所得税":             "Income Tax Expense",
    "本期净利润":         "Net Income",
    "净利润":             "Net Income",
    "归属于母公司股东":   "Net Income Common Stockholders",
    "基本每股盈利":       "Basic EPS",
    "摊薄每股盈利":       "Diluted EPS",
    # Balance sheet (资产负债表)
    "资产总计":           "Total Assets",
    "流动资产合计":       "Current Assets",
    "非流动资产合计":     "Non-Current Assets",
    "现金及现金等价物":   "Cash And Cash Equivalents",
    "货币资金":           "Cash And Cash Equivalents",
    "应收账款":           "Accounts Receivable",
    "存货":               "Inventory",
    "物业、厂房及设备":   "Property Plant And Equipment",
    "投资物业":           "Investment Property",
    "无形资产":           "Intangible Assets",
    "商誉":               "Goodwill",
    "负债合计":           "Total Liabilities",
    "流动负债合计":       "Current Liabilities",
    "非流动负债合计":     "Non-Current Liabilities",
    "应付账款":           "Accounts Payable",
    "短期借款":           "Short Term Debt",
    "长期借款":           "Long Term Debt",
    "权益总额":           "Total Equity",
    "所有者权益合计":     "Total Equity",
    "归属于母公司股东权益合计": "Stockholders Equity",
    "股本":               "Common Stock",
    "储备":               "Reserves",
    "未分配利润":         "Retained Earnings",
    "少数股东权益":       "Minority Interest",
    # Cash flow (现金流量表)
    "经营活动产生的现金流量净额": "Operating Cash Flow",
    "经营业务产生的现金净额":     "Operating Cash Flow",
    "投资活动产生的现金流量净额": "Investing Cash Flow",
    "投资活动现金净额":           "Investing Cash Flow",
    "筹资活动产生的现金流量净额": "Financing Cash Flow",
    "融资活动现金净额":           "Financing Cash Flow",
    "购建固定资产支付的现金":     "Capital Expenditure",
    "现金及现金等价物净增加额":   "Net Change In Cash",
}

# akshare `symbol` parameter value (Chinese statement name) -> our statement_type
_AKSHARE_SYMBOL_MAP = {
    "利润表":     "income",
    "资产负债表": "balance",
    "现金流量表": "cashflow",
}


def _ticker_to_akshare(ticker: str) -> str:
    """0700.HK -> 00700 (akshare uses 5-digit zero-padded HKEX codes)."""
    code = ticker.split(".")[0].lstrip("0") or "0"
    return code.zfill(5)


def _fetch_akshare(ticker: str) -> dict[str, list[dict]]:
    """Pull all three statements from akshare's HK report endpoint, annual only."""
    out: dict[str, list[dict]] = {"income": [], "balance": [], "cashflow": []}
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare not available")
        return out

    ak_code = _ticker_to_akshare(ticker)
    for symbol_cn, stype in _AKSHARE_SYMBOL_MAP.items():
        try:
            df = ak.stock_financial_hk_report_em(
                stock=ak_code, symbol=symbol_cn, indicator="年度"
            )
        except Exception as e:
            logger.warning("akshare %s [%s -> %s]: %s", symbol_cn, ticker, ak_code, e)
            continue
        if df is None or df.empty:
            continue

        # Schema (akshare 1.13+): SECUCODE, SECURITY_CODE, ..., REPORT_DATE,
        # DATE_TYPE_CODE, FISCAL_YEAR, START_DATE, STD_ITEM_CODE, STD_ITEM_NAME, AMOUNT
        rd_col = "REPORT_DATE" if "REPORT_DATE" in df.columns else None
        name_col = "STD_ITEM_NAME" if "STD_ITEM_NAME" in df.columns else None
        val_col = "AMOUNT" if "AMOUNT" in df.columns else None
        if not (rd_col and name_col and val_col):
            logger.warning("akshare %s unexpected columns: %s",
                            symbol_cn, list(df.columns)[:8])
            continue

        by_period: dict[str, dict[str, float]] = {}
        for record in df.to_dict("records"):
            period_end = str(record.get(rd_col) or "")[:10]
            if not period_end:
                continue
            raw_name = str(record.get(name_col) or "").strip()
            if not raw_name:
                continue
            mapped = AKSHARE_LINE_ITEM_MAP.get(raw_name, raw_name)
            v = _to_finite(record.get(val_col))
            if v is None:
                continue
            by_period.setdefault(period_end, {})[mapped] = v

        for period_end, items in by_period.items():
            if not items:
                continue
            out[stype].append({
                "period_end_date": period_end,
                "period_type": "annual",
                "source": "akshare",
                "currency": None,  # akshare doesn't expose currency on this endpoint
                "line_items": items,
            })

    return out


if __name__ == "__main__":
    # Smoke test: python -m scrapers.financial_statements_scraper 0700.HK
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t = sys.argv[1] if len(sys.argv) > 1 else "0700.HK"
    r = fetch_statements(t)
    for stype, rows in r.items():
        print(f"\n{stype}: {len(rows)} period(s)")
        for row in rows[:3]:
            print(f"  {row['period_end_date']} [{row['period_type']}, src={row['source']}, "
                   f"ccy={row['currency']}, {len(row['line_items'])} items]")
            for k, v in list(row["line_items"].items())[:4]:
                print(f"    {k}: {v:,.0f}")
