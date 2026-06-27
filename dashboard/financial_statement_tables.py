"""Chart + table builders for the Stock Research Section 3b financial-statements UI.

Kept separate from stock_research_callbacks.py (which is already large) so the
formatting logic lives in one focused module.

Public API:
    # Existing — headline chart + full DataTable + earnings tab + empty state
    build_statement_chart(statement_type, rows) -> go.Figure
    build_statement_table(statement_type, rows) -> dash_table.DataTable
    build_earnings_chart(income_rows) -> go.Figure
    build_earnings_table(income_rows) -> dash_table.DataTable
    build_unavailable_state(statement_type, ticker=None) -> html.Div

    # Analyst-friendly redesign (Section 3b layers 1-3 — see plan)
    build_kpi_strip(statement_type, rows) -> html.Div
    build_analyst_table(statement_type, rows) -> html.Div
    build_math_walkthrough(statement_type, rows) -> html.Div
"""
from typing import Optional

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import dash_table, html

from dashboard import theme as T
from dashboard.charts import _empty_fig

# Top-3 line items to feature in each statement's headline chart. Aliases account
# for yfinance vs akshare key differences (we try each in order, take first match).
_CHART_HEADLINE_ITEMS = {
    "income": [
        ("Revenue", ["Total Revenue", "Operating Revenue"]),
        ("Gross Profit", ["Gross Profit"]),
        ("Net Income", ["Net Income", "Net Income Common Stockholders",
                          "Net Income From Continuing Operation Net Minority Interest"]),
    ],
    "balance": [
        ("Total Assets", ["Total Assets"]),
        ("Total Liabilities", ["Total Liabilities", "Total Liabilities Net Minority Interest"]),
        ("Stockholders Equity", ["Stockholders Equity", "Total Equity",
                                   "Total Equity Gross Minority Interest"]),
    ],
    "cashflow": [
        ("Operating CF", ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"]),
        ("Investing CF", ["Investing Cash Flow", "Cash Flow From Continuing Investing Activities"]),
        ("Financing CF", ["Financing Cash Flow", "Cash Flow From Continuing Financing Activities"]),
    ],
}

_STATEMENT_TITLES = {
    "income": "Income Statement",
    "balance": "Balance Sheet",
    "cashflow": "Cash Flow Statement",
}

# Bar colors per series (consistent within each statement_type)
_SERIES_COLORS = [T.PRIMARY, T.ACCENT_2, T.SUCCESS]


def build_statement_chart(statement_type: str, rows: list[dict]) -> go.Figure:
    """Grouped-bar chart of top-3 line items across periods. Rows newest-first."""
    if not rows:
        return _empty_fig(_STATEMENT_TITLES.get(statement_type, statement_type),
                          "No data available")

    # Sort oldest-first for charts (left-to-right time progression)
    rows_chrono = sorted(rows, key=lambda r: r["period_end_date"])
    periods = [r["period_end_date"] for r in rows_chrono]

    fig = go.Figure()
    headline = _CHART_HEADLINE_ITEMS.get(statement_type, [])
    any_series = False
    for (label, aliases), color in zip(headline, _SERIES_COLORS):
        values = [_first_match(r["line_items"], aliases) for r in rows_chrono]
        if not any(v is not None for v in values):
            continue
        # Display in billions
        y_b = [v / 1e9 if v is not None else None for v in values]
        fig.add_trace(go.Bar(
            x=periods, y=y_b, name=label,
            marker_color=color, marker_line_width=0,
            text=[f"{v:.1f}B" if v is not None else "" for v in y_b],
            textposition="outside",
            hovertemplate=f"<b>{label}</b><br>%{{x}}<br>%{{y:.2f}}B<extra></extra>",
        ))
        any_series = True

    if not any_series:
        return _empty_fig(_STATEMENT_TITLES.get(statement_type, statement_type),
                          "Headline items not found in available line items")

    ccy = rows_chrono[-1].get("currency") or ""
    title = f"{_STATEMENT_TITLES[statement_type]} — top items ({ccy} bn)" if ccy \
            else f"{_STATEMENT_TITLES[statement_type]} — top items (bn)"

    fig.update_layout(T.chart_layout(title),
                      barmode="group",
                      height=300, margin=dict(t=50, b=40, l=60, r=20),
                      legend=dict(orientation="h", yanchor="bottom", y=-0.18,
                                   xanchor="center", x=0.5, bgcolor="rgba(255,255,255,0)"))
    fig.update_xaxes(tickformat="%Y-%m")
    return fig


def build_statement_table(statement_type: str, rows: list[dict]) -> html.Div:
    """Full statement as a DataTable: line items as rows, period-end dates as columns."""
    if not rows:
        return html.P("No data available for this statement.",
                       className="text-muted small fst-italic")

    # Period-end dates form the value columns (newest-first matches user expectation)
    rows_sorted = sorted(rows, key=lambda r: r["period_end_date"], reverse=True)
    period_cols = [r["period_end_date"] for r in rows_sorted]

    # Union of all line items, preserving the order of first appearance
    seen = {}
    for r in rows_sorted:
        for k in r["line_items"].keys():
            seen.setdefault(k, len(seen))
    line_items_ordered = list(seen.keys())

    # Build row data: one row per line item, columns are period dates
    data = []
    for li in line_items_ordered:
        row = {"line_item": li}
        for r in rows_sorted:
            v = r["line_items"].get(li)
            row[r["period_end_date"]] = _fmt_money(v)
        data.append(row)

    columns = [{"name": "Line Item", "id": "line_item"}]
    for p in period_cols:
        columns.append({"name": p, "id": p})

    return dash_table.DataTable(
        data=data,
        columns=columns,
        page_size=50,
        style_table={"overflowX": "auto"},
        style_cell={**T.DATATABLE_CELL,
                     "minWidth": "110px", "whiteSpace": "normal"},
        style_cell_conditional=[
            {"if": {"column_id": "line_item"},
             "textAlign": "left", "fontWeight": "500",
             "fontFamily": "Inter, sans-serif",
             "color": T.TEXT, "minWidth": "260px"},
        ],
        style_header=T.DATATABLE_HEADER,
    )


def build_earnings_chart(income_rows: list[dict]) -> go.Figure:
    """Earnings tab: EPS (basic) over periods + YoY change as secondary."""
    if not income_rows:
        return _empty_fig("Earnings", "No income data available")

    rows_chrono = sorted(income_rows, key=lambda r: r["period_end_date"])
    periods = [r["period_end_date"] for r in rows_chrono]
    eps = [_first_match(r["line_items"], ["Basic EPS", "Diluted EPS"])
           for r in rows_chrono]

    if not any(v is not None for v in eps):
        return _empty_fig("Earnings", "EPS not available in income data")

    fig = go.Figure(go.Bar(
        x=periods, y=eps,
        marker_color=[T.PRICE_UP if (e or 0) >= 0 else T.PRICE_DOWN for e in eps],
        marker_line_width=0,
        text=[f"{e:.2f}" if e is not None else "" for e in eps],
        textposition="outside",
        hovertemplate="<b>EPS</b><br>%{x}<br>%{y:.2f}<extra></extra>",
    ))
    ccy = rows_chrono[-1].get("currency") or ""
    title = f"Earnings per Share ({ccy})" if ccy else "Earnings per Share"
    fig.update_layout(T.chart_layout(title),
                      height=300, margin=dict(t=50, b=40, l=60, r=20))
    fig.update_xaxes(tickformat="%Y-%m")
    return fig


def build_earnings_table(income_rows: list[dict]) -> html.Div:
    """Earnings tab: compact table of EPS, revenue, net income, period_type per period."""
    if not income_rows:
        return html.P("No income data available.",
                       className="text-muted small fst-italic")
    rows_sorted = sorted(income_rows, key=lambda r: r["period_end_date"], reverse=True)
    data = []
    for r in rows_sorted:
        items = r["line_items"]
        data.append({
            "period": r["period_end_date"],
            "type": r.get("period_type", "?"),
            "currency": r.get("currency") or "—",
            "revenue": _fmt_money(_first_match(items, ["Total Revenue", "Operating Revenue"])),
            "net_income": _fmt_money(_first_match(items, ["Net Income", "Net Income Common Stockholders"])),
            "eps_basic": _fmt_eps(_first_match(items, ["Basic EPS"])),
            "eps_diluted": _fmt_eps(_first_match(items, ["Diluted EPS"])),
        })
    return dash_table.DataTable(
        data=data,
        columns=[
            {"name": "Period End", "id": "period"},
            {"name": "Type", "id": "type"},
            {"name": "Ccy", "id": "currency"},
            {"name": "Revenue", "id": "revenue"},
            {"name": "Net Income", "id": "net_income"},
            {"name": "EPS Basic", "id": "eps_basic"},
            {"name": "EPS Diluted", "id": "eps_diluted"},
        ],
        page_size=20,
        style_cell=T.DATATABLE_CELL,
        style_cell_conditional=[
            {"if": {"column_id": "period"}, "textAlign": "left",
             "fontWeight": "600", "color": T.PRIMARY},
            {"if": {"column_id": "type"}, "textAlign": "left", "color": T.TEXT_MUTED},
            {"if": {"column_id": "currency"}, "textAlign": "center", "color": T.TEXT_MUTED},
        ],
        style_header=T.DATATABLE_HEADER,
    )


def build_unavailable_state(statement_type: str,
                              ticker: str | None = None) -> html.Div:
    # Market-aware fallback hint — US users should see "SEC filings", HK
    # users "HKEX disclosures". Falls back to a generic line when ticker
    # is not provided.
    if ticker:
        from utils.market import market_of_ticker
        if market_of_ticker(ticker) == "US":
            fallback_blurb = "check the company's SEC filings directly."
        else:
            fallback_blurb = "check the company's HKEX disclosures directly."
    else:
        fallback_blurb = "check the official filings directly."
    return html.Div([
        html.P(f"{_STATEMENT_TITLES.get(statement_type, statement_type)} not available "
                "for this ticker.",
                style={"color": T.TEXT_MUTED, "fontSize": "0.9rem",
                       "marginBottom": "4px"}),
        html.P(f"Both yfinance and akshare returned empty data. "
                f"Try a different ticker or {fallback_blurb}",
                className="small text-muted fst-italic"),
    ], style={"padding": "32px 16px", "textAlign": "center"})


# ============== helpers ==============

def _first_match(items: dict, aliases: list[str]) -> Optional[float]:
    """Return the first non-None value from items where the key matches any alias."""
    for a in aliases:
        v = items.get(a)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _fmt_money(v) -> str:
    """Format big numbers with B/M suffix; round to 2 d.p."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    a = abs(f)
    if a >= 1e9:
        return f"{f/1e9:,.2f}B"
    if a >= 1e6:
        return f"{f/1e6:,.2f}M"
    if a >= 1e3:
        return f"{f/1e3:,.2f}K"
    return f"{f:,.2f}"


def _fmt_eps(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


# ============================================================================
# Analyst-friendly layered display (Section 3b layers 1-3 — see plan)
# ----------------------------------------------------------------------------
# CANONICAL_LAYOUT defines the hierarchical ordering of the compact analyst
# table per statement type. Each entry:
#   label       — display name (column 1)
#   aliases     — list of keys to try in `line_items` (first non-None wins —
#                 covers yfinance / akshare drift)
#   indent      — 0 for subtotal / standalone, 1 for sub-item under parent
#   subtotal    — True → bold + tinted background row
#   denominator — common-size base: "revenue" / "total_assets" / "ocf" / None
#                 (None ⇒ no % column for this row)
#   show_negate — True ⇒ display as negative (parens + muted) even when stored
#                 positive (cost items, opex, capex, taxes, dividends, etc.)
#   eps         — True ⇒ format as 2-d.p. number not money (Diluted EPS only)
# ============================================================================

_REVENUE_ALIASES = ["Total Revenue", "Operating Revenue"]
_TOTAL_ASSETS_ALIASES = ["Total Assets"]
_OCF_ALIASES = ["Operating Cash Flow",
                  "Cash Flow From Continuing Operating Activities"]

CANONICAL_LAYOUT: dict[str, list[dict]] = {
    "income": [
        {"label": "Revenue", "aliases": _REVENUE_ALIASES,
         "indent": 0, "subtotal": True, "denominator": "revenue"},
        {"label": "Cost of Revenue",
         "aliases": ["Cost Of Revenue", "Reconciled Cost Of Revenue"],
         "indent": 1, "subtotal": False, "denominator": "revenue",
         "show_negate": True},
        {"label": "Gross Profit", "aliases": ["Gross Profit"],
         "indent": 0, "subtotal": True, "denominator": "revenue"},
        {"label": "Research & Development",
         "aliases": ["Research And Development"],
         "indent": 1, "subtotal": False, "denominator": "revenue",
         "show_negate": True},
        {"label": "Selling, General & Admin",
         "aliases": ["Selling General And Administration"],
         "indent": 1, "subtotal": False, "denominator": "revenue",
         "show_negate": True},
        {"label": "Operating Income",
         "aliases": ["Operating Income", "Total Operating Income As Reported"],
         "indent": 0, "subtotal": True, "denominator": "revenue"},
        {"label": "Other Income / Expense",
         "aliases": ["Other Non Operating Income Expenses",
                      "Other Income Expense"],
         "indent": 1, "subtotal": False, "denominator": "revenue"},
        {"label": "Pretax Income", "aliases": ["Pretax Income"],
         "indent": 0, "subtotal": True, "denominator": "revenue"},
        {"label": "Tax Provision", "aliases": ["Tax Provision"],
         "indent": 1, "subtotal": False, "denominator": "revenue",
         "show_negate": True},
        {"label": "Net Income",
         "aliases": ["Net Income", "Net Income Common Stockholders",
                      "Net Income From Continuing Operation Net Minority Interest"],
         "indent": 0, "subtotal": True, "denominator": "revenue"},
        {"label": "Diluted EPS", "aliases": ["Diluted EPS", "Basic EPS"],
         "indent": 1, "subtotal": False, "denominator": None, "eps": True},
    ],
    "balance": [
        {"label": "Cash & Equivalents",
         "aliases": ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
         "indent": 1, "subtotal": False, "denominator": "total_assets"},
        {"label": "Accounts Receivable",
         "aliases": ["Accounts Receivable", "Receivables"],
         "indent": 1, "subtotal": False, "denominator": "total_assets"},
        {"label": "Inventory", "aliases": ["Inventory"],
         "indent": 1, "subtotal": False, "denominator": "total_assets"},
        {"label": "Current Assets", "aliases": ["Current Assets"],
         "indent": 0, "subtotal": True, "denominator": "total_assets"},
        {"label": "Property, Plant & Equipment",
         "aliases": ["Net PPE", "Property Plant And Equipment Net",
                      "Property Plant And Equipment"],
         "indent": 1, "subtotal": False, "denominator": "total_assets"},
        {"label": "Goodwill & Intangibles",
         "aliases": ["Goodwill And Other Intangible Assets", "Goodwill"],
         "indent": 1, "subtotal": False, "denominator": "total_assets"},
        {"label": "Total Assets", "aliases": _TOTAL_ASSETS_ALIASES,
         "indent": 0, "subtotal": True, "denominator": "total_assets"},
        {"label": "Current Liabilities", "aliases": ["Current Liabilities"],
         "indent": 1, "subtotal": False, "denominator": "total_assets"},
        {"label": "Long-Term Debt",
         "aliases": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"],
         "indent": 1, "subtotal": False, "denominator": "total_assets"},
        {"label": "Total Liabilities",
         "aliases": ["Total Liabilities Net Minority Interest", "Total Liabilities"],
         "indent": 0, "subtotal": True, "denominator": "total_assets"},
        {"label": "Retained Earnings", "aliases": ["Retained Earnings"],
         "indent": 1, "subtotal": False, "denominator": "total_assets"},
        {"label": "Stockholders Equity",
         "aliases": ["Stockholders Equity", "Total Equity Gross Minority Interest",
                      "Total Equity"],
         "indent": 0, "subtotal": True, "denominator": "total_assets"},
    ],
    "cashflow": [
        {"label": "Net Income (start)",
         "aliases": ["Net Income From Continuing Operations",
                      "Net Income", "Net Income Including Noncontrolling Interests"],
         "indent": 1, "subtotal": False, "denominator": "ocf"},
        {"label": "Depreciation & Amortization",
         "aliases": ["Depreciation And Amortization",
                      "Depreciation Amortization Depletion"],
         "indent": 1, "subtotal": False, "denominator": "ocf"},
        {"label": "Change in Working Capital",
         "aliases": ["Change In Working Capital", "Changes In Working Capital"],
         "indent": 1, "subtotal": False, "denominator": "ocf"},
        {"label": "Operating Cash Flow", "aliases": _OCF_ALIASES,
         "indent": 0, "subtotal": True, "denominator": "ocf"},
        {"label": "Capital Expenditure",
         "aliases": ["Capital Expenditure", "Purchase Of PPE"],
         "indent": 1, "subtotal": False, "denominator": "ocf",
         "show_negate": True},
        {"label": "Investing Cash Flow",
         "aliases": ["Investing Cash Flow",
                      "Cash Flow From Continuing Investing Activities"],
         "indent": 0, "subtotal": True, "denominator": "ocf"},
        {"label": "Free Cash Flow",
         "aliases": ["Free Cash Flow"],
         "indent": 0, "subtotal": True, "denominator": "ocf",
         "derived_fcf": True},     # special: derive OCF - CapEx when missing
        {"label": "Dividends Paid",
         "aliases": ["Cash Dividends Paid", "Common Stock Dividend Paid"],
         "indent": 1, "subtotal": False, "denominator": "ocf",
         "show_negate": True},
        {"label": "Stock Buybacks",
         "aliases": ["Repurchase Of Capital Stock", "Common Stock Payments"],
         "indent": 1, "subtotal": False, "denominator": "ocf",
         "show_negate": True},
        {"label": "Financing Cash Flow",
         "aliases": ["Financing Cash Flow",
                      "Cash Flow From Continuing Financing Activities"],
         "indent": 0, "subtotal": True, "denominator": "ocf"},
    ],
}

# Cap displayed periods in the analyst table at 4 — full set lives under the
# advanced toggle. 4 is enough to read trend without the row hitting overflow.
_ANALYST_MAX_PERIODS = 4

# Common-size column header per statement (denominator clarifier).
_COMMON_SIZE_HEADER = {
    "income":   "% Rev",
    "balance":  "% Assets",
    "cashflow": "% OCF",
}


# ----- math primitives -----

def _yoy_pct(latest, prior) -> Optional[float]:
    """Return (latest - prior) / |prior| as a fraction. None when undefined."""
    try:
        l, p = float(latest), float(prior)
    except (TypeError, ValueError):
        return None
    if p == 0:
        return None
    return (l - p) / abs(p)


def _common_size_pct(value, denominator) -> Optional[float]:
    """Return value / denominator as a fraction. None when undefined."""
    try:
        v, d = float(value), float(denominator)
    except (TypeError, ValueError):
        return None
    if d == 0:
        return None
    return v / d


def _fmt_signed_money(v, *, force_negate: bool = False) -> str:
    """Money formatter that wraps negatives in parens. force_negate=True flips
    a positive cost-style value (stored as +210B for COGS) to the displayed
    `(210.4B)` form."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if force_negate:
        f = -abs(f)
    a = abs(f)
    if a >= 1e9:
        s = f"{a / 1e9:,.2f}B"
    elif a >= 1e6:
        s = f"{a / 1e6:,.2f}M"
    elif a >= 1e3:
        s = f"{a / 1e3:,.2f}K"
    else:
        s = f"{a:,.2f}"
    return f"({s})" if f < 0 else s


def _fmt_pct(frac, decimals: int = 1) -> str:
    """Unsigned percent (use for absolute ratios like margin %). For YoY
    deltas where the +/- direction matters, use `_fmt_pct_signed`."""
    if frac is None:
        return "—"
    try:
        return f"{float(frac) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct_signed(frac, decimals: int = 1) -> str:
    """Always-signed percent (used in YoY column)."""
    if frac is None:
        return "—"
    try:
        return f"{float(frac) * 100:+.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def _color_for_yoy(frac) -> str:
    """Standard finance convention: green positive, red negative — NOT the
    CN/HK PRICE_UP/PRICE_DOWN convention. YoY growth has universal semantics."""
    if frac is None:
        return T.TEXT_FAINT
    try:
        f = float(frac)
    except (TypeError, ValueError):
        return T.TEXT_FAINT
    if f > 0:
        return T.SUCCESS
    if f < 0:
        return T.DANGER
    return T.TEXT_MUTED


def _resolve_denominator(items: dict, denominator: Optional[str]) -> Optional[float]:
    """Look up the common-size base value from the period's line_items."""
    if denominator == "revenue":
        return _first_match(items, _REVENUE_ALIASES)
    if denominator == "total_assets":
        return _first_match(items, _TOTAL_ASSETS_ALIASES)
    if denominator == "ocf":
        return _first_match(items, _OCF_ALIASES)
    return None


def _derive_fcf(items: dict) -> Optional[float]:
    """Free Cash Flow = OCF − |CapEx|. Returns None if either input is missing."""
    ocf = _first_match(items, _OCF_ALIASES)
    capex = _first_match(items, ["Capital Expenditure", "Purchase Of PPE"])
    if ocf is None or capex is None:
        return None
    return ocf - abs(capex)


# Stub-period filter aliases — used to drop sparse quarterly filings that only
# contain EPS + share counts (no top-line revenue / no balance / no OCF). The
# analyst layers need *substantive* periods, not stubs. Specifically: yfinance
# often returns recent partial filings like 2026-03-31 with 4 keys (EPS only)
# alongside the real annual filing — the partial would otherwise force every
# KPI / math equation to render against missing data.
_SUBSTANTIVE_ANCHOR = {
    "income":   _REVENUE_ALIASES,
    "balance":  _TOTAL_ASSETS_ALIASES,
    "cashflow": _OCF_ALIASES,
}


def _filter_substantive(statement_type: str, rows: list[dict]) -> list[dict]:
    """Keep only periods whose line_items contain the canonical denominator
    for this statement type. Falls back to the original rows if filtering
    would leave nothing (defensive — never want to render an empty section
    because every period was atypical)."""
    anchor = _SUBSTANTIVE_ANCHOR.get(statement_type)
    if not anchor or not rows:
        return rows
    kept = [r for r in rows
             if _first_match(r.get("line_items", {}), anchor) is not None]
    return kept if kept else rows


# ----- Layer 1: KPI strip -----

# Per-statement KPI cards. Each entry produces one card.
# kind: "money" (B/M format) | "pct" (multiplied by 100) | "ratio" (raw 2dp) | "eps"
# label_key matches an i18n string the layout-level callback supplies. Cards
# render in the order listed. YoY chip computed against the immediately prior
# period when available.
_KPI_DEFS = {
    "income": [
        {"label": "Revenue", "kind": "money",
         "extract": lambda i: _first_match(i, _REVENUE_ALIASES)},
        {"label": "Gross Margin", "kind": "pct",
         "extract": lambda i: _common_size_pct(_first_match(i, ["Gross Profit"]),
                                                  _first_match(i, _REVENUE_ALIASES))},
        {"label": "Operating Margin", "kind": "pct",
         "extract": lambda i: _common_size_pct(
                _first_match(i, ["Operating Income",
                                   "Total Operating Income As Reported"]),
                _first_match(i, _REVENUE_ALIASES))},
        {"label": "Diluted EPS", "kind": "eps",
         "extract": lambda i: _first_match(i, ["Diluted EPS", "Basic EPS"])},
    ],
    "balance": [
        {"label": "Total Assets", "kind": "money",
         "extract": lambda i: _first_match(i, _TOTAL_ASSETS_ALIASES)},
        {"label": "Stockholders Equity", "kind": "money",
         "extract": lambda i: _first_match(i, ["Stockholders Equity",
                                                 "Total Equity Gross Minority Interest",
                                                 "Total Equity"])},
        {"label": "Debt / Equity", "kind": "ratio",
         "extract": lambda i: _common_size_pct(
                _first_match(i, ["Total Debt"]) or
                ((_first_match(i, ["Long Term Debt"]) or 0)
                 + (_first_match(i, ["Current Debt", "Short Term Debt"]) or 0)
                 if (_first_match(i, ["Long Term Debt"]) is not None or
                      _first_match(i, ["Current Debt", "Short Term Debt"]) is not None)
                 else None),
                _first_match(i, ["Stockholders Equity",
                                   "Total Equity Gross Minority Interest"]))},
        {"label": "Cash & Equivalents", "kind": "money",
         "extract": lambda i: _first_match(i, ["Cash And Cash Equivalents",
                                                 "Cash Cash Equivalents And Short Term Investments"])},
    ],
    "cashflow": [
        {"label": "Operating Cash Flow", "kind": "money",
         "extract": lambda i: _first_match(i, _OCF_ALIASES)},
        {"label": "Free Cash Flow", "kind": "money",
         "extract": lambda i: (_first_match(i, ["Free Cash Flow"])
                                or _derive_fcf(i))},
        {"label": "CapEx", "kind": "money",
         "extract": lambda i: _first_match(i, ["Capital Expenditure",
                                                 "Purchase Of PPE"])},
        {"label": "Dividends Paid", "kind": "money",
         "extract": lambda i: _first_match(i, ["Cash Dividends Paid",
                                                 "Common Stock Dividend Paid"])},
    ],
}


def _fmt_kpi(value, kind: str) -> str:
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    if kind == "money":
        # CapEx, Dividends stored positive but represent outflows — leave the
        # sign as stored; the card label clarifies the direction.
        return _fmt_signed_money(f)
    if kind == "pct":
        return f"{f * 100:.1f}%"
    if kind == "ratio":
        return f"{f:.2f}×"
    if kind == "eps":
        return f"{f:.2f}"
    return str(value)


def build_kpi_strip(statement_type: str, rows: list[dict]) -> html.Div:
    """4 KPI cards across the top of the tab. Latest period values + YoY chip
    against the immediately prior period."""
    defs = _KPI_DEFS.get(statement_type, [])
    rows = _filter_substantive(statement_type, rows)
    if not rows or not defs:
        return html.Div()
    rows_sorted = sorted(rows, key=lambda r: r["period_end_date"], reverse=True)
    latest_items = rows_sorted[0]["line_items"]
    prior_items = rows_sorted[1]["line_items"] if len(rows_sorted) > 1 else {}
    latest_label = rows_sorted[0]["period_end_date"]

    cards = []
    for d in defs:
        try:
            latest_val = d["extract"](latest_items)
        except Exception:
            latest_val = None
        try:
            prior_val = d["extract"](prior_items) if prior_items else None
        except Exception:
            prior_val = None
        yoy = _yoy_pct(latest_val, prior_val) \
              if latest_val is not None and prior_val is not None else None
        yoy_chip = html.Span(
            _fmt_pct_signed(yoy) if yoy is not None else "",
            style={"color": _color_for_yoy(yoy),
                    "fontSize": "0.72rem", "fontWeight": "600",
                    "marginLeft": "6px"},
        )
        cards.append(dbc.Col(dbc.Card(dbc.CardBody([
            html.Div(d["label"],
                      style={"color": T.TEXT_MUTED, "fontSize": "0.72rem",
                              "fontWeight": "600", "textTransform": "uppercase",
                              "letterSpacing": "0.06em"}),
            html.Div([
                html.Span(_fmt_kpi(latest_val, d["kind"]),
                            style={"fontSize": T.FONT_HERO_SM,
                                    "fontWeight": "700", "color": T.TEXT,
                                    "lineHeight": "1.1"}),
                yoy_chip,
            ], style={"marginTop": "4px"}),
            html.Div(f"as of {latest_label}",
                      style={"color": T.TEXT_FAINT,
                              "fontSize": "0.68rem", "marginTop": "2px"}),
        ], style={"padding": "10px 14px"}),
            style={**T.CARD_STYLE_SOFT}), xs=12, sm=6, md=3))
    return dbc.Row(cards, className="g-2 mb-3")


# ----- Layer 2: compact analyst table -----

def build_analyst_table(statement_type: str, rows: list[dict]) -> html.Div:
    """Hierarchical compact table — only canonical items, subtotals bold/tinted,
    sub-items indented, common-size column on right, YoY column.

    Uses an html.Table (not Dash DataTable) for full row-styling control —
    DataTable doesn't cleanly support the indent + tinted-subtotal pattern."""
    layout = CANONICAL_LAYOUT.get(statement_type, [])
    rows = _filter_substantive(statement_type, rows)
    if not rows or not layout:
        return html.Div()

    rows_sorted = sorted(rows, key=lambda r: r["period_end_date"],
                          reverse=True)[:_ANALYST_MAX_PERIODS]
    periods = [r["period_end_date"] for r in rows_sorted]
    items_per_period = [r["line_items"] for r in rows_sorted]
    latest_items = items_per_period[0]
    prior_items = items_per_period[1] if len(items_per_period) > 1 else {}

    # Header row
    common_size_header = _COMMON_SIZE_HEADER.get(statement_type, "%")
    header_cells = [html.Th("Line Item", style={"textAlign": "left",
                                                    "padding": "6px 10px",
                                                    "fontWeight": "600",
                                                    "fontSize": "0.78rem",
                                                    "color": T.TEXT_MUTED,
                                                    "position": "sticky",
                                                    "left": 0,
                                                    "background": T.CARD_BG,
                                                    "zIndex": 2})]
    for p in periods:
        header_cells.append(html.Th(p, style={"textAlign": "right",
                                                "padding": "6px 10px",
                                                "fontWeight": "600",
                                                "fontSize": "0.78rem",
                                                "color": T.TEXT_MUTED}))
    header_cells.append(html.Th("YoY %", style={"textAlign": "right",
                                                  "padding": "6px 10px",
                                                  "fontWeight": "600",
                                                  "fontSize": "0.78rem",
                                                  "color": T.TEXT_MUTED}))
    header_cells.append(html.Th(common_size_header,
                                  style={"textAlign": "right",
                                          "padding": "6px 10px",
                                          "fontWeight": "600",
                                          "fontSize": "0.78rem",
                                          "color": T.TEXT_MUTED}))

    body_rows = []
    for entry in layout:
        is_subtotal = entry.get("subtotal", False)
        indent = entry.get("indent", 0)
        show_negate = entry.get("show_negate", False)
        is_eps = entry.get("eps", False)
        denom_key = entry.get("denominator")
        derive_fcf = entry.get("derived_fcf", False)

        # Pull per-period values, with FCF derivation fallback
        per_period_values = []
        for items in items_per_period:
            v = _first_match(items, entry["aliases"])
            if v is None and derive_fcf:
                v = _derive_fcf(items)
            per_period_values.append(v)

        # Skip the row entirely if NO period has a value AND the item isn't a
        # canonical subtotal — keeps the table tight on sparse-data tickers.
        # Always render subtotals so the structure is legible even with gaps.
        if not is_subtotal and all(v is None for v in per_period_values):
            continue

        latest_val = per_period_values[0]
        prior_val = per_period_values[1] if len(per_period_values) > 1 else None
        yoy = _yoy_pct(latest_val, prior_val) \
              if latest_val is not None and prior_val is not None else None
        denom_val = _resolve_denominator(latest_items, denom_key) \
                    if denom_key else None
        cs_pct = _common_size_pct(latest_val, denom_val) \
                  if denom_key and latest_val is not None else None

        # Row styling
        row_style = {}
        cell_style_base = {"padding": "5px 10px", "fontSize": "0.82rem",
                            "borderTop": f"1px solid {T.BORDER}"}
        if is_subtotal:
            row_style["background"] = T.PRIMARY_SOFT
            cell_style_base = {**cell_style_base, "fontWeight": "700",
                                "color": T.TEXT}
        elif indent > 0:
            cell_style_base = {**cell_style_base, "color": T.TEXT_MUTED}

        # Label cell (sticky-left)
        derived_marker = (html.Span(" (derived)",
                                       style={"color": T.TEXT_FAINT,
                                               "fontSize": "0.7rem"})
                            if derive_fcf and per_period_values[0] is not None
                            and _first_match(latest_items, entry["aliases"]) is None
                            else "")
        label_cell = html.Td(
            html.Span([
                (" " * 4 if indent > 0 else ""),
                entry["label"],
                derived_marker,
            ]),
            style={**cell_style_base, "textAlign": "left",
                    "position": "sticky", "left": 0, "background":
                    (T.PRIMARY_SOFT if is_subtotal else T.CARD_BG),
                    "zIndex": 1, "minWidth": "220px"},
        )

        # Period value cells
        value_cells = []
        for v in per_period_values:
            if v is None:
                text = "—"
                color = T.TEXT_FAINT
            elif is_eps:
                text = f"{float(v):.2f}"
                color = T.TEXT
            else:
                text = _fmt_signed_money(v, force_negate=show_negate)
                color = (T.DANGER if (show_negate or (isinstance(v, (int, float))
                                                          and v < 0))
                          else T.TEXT)
            value_cells.append(html.Td(text,
                                          style={**cell_style_base,
                                                  "textAlign": "right",
                                                  "fontFamily": "ui-monospace, monospace",
                                                  "color": color}))

        # YoY cell
        yoy_cell = html.Td(_fmt_pct_signed(yoy),
                              style={**cell_style_base, "textAlign": "right",
                                      "color": _color_for_yoy(yoy),
                                      "fontFamily": "ui-monospace, monospace"})

        # Common-size cell
        if is_eps or cs_pct is None:
            cs_text = "—" if not is_eps else ""
            cs_color = T.TEXT_FAINT
        else:
            cs_text = f"{cs_pct * 100:.1f}%"
            cs_color = T.TEXT_MUTED
        cs_cell = html.Td(cs_text,
                            style={**cell_style_base, "textAlign": "right",
                                    "color": cs_color,
                                    "fontFamily": "ui-monospace, monospace"})

        body_rows.append(html.Tr([label_cell, *value_cells, yoy_cell, cs_cell],
                                    style=row_style))

    table = html.Table(
        [html.Thead(html.Tr(header_cells,
                              style={"background": T.CARD_BG_SOFT,
                                      "borderBottom": f"2px solid {T.BORDER_STRONG}"})),
         html.Tbody(body_rows)],
        style={"width": "100%", "borderCollapse": "collapse",
                "fontFamily": "Inter, sans-serif"},
    )
    # Currency note
    ccy = rows_sorted[0].get("currency") or ""
    caption = (f"Values in {ccy} unless EPS / margins / ratios; latest "
                f"{len(periods)} periods shown — see 'every line item' below "
                "for full history.")
    return html.Div([
        html.Div(table, style={"overflowX": "auto"}),
        html.P(caption, style={"color": T.TEXT_FAINT,
                                  "fontSize": "0.72rem",
                                  "marginTop": "6px",
                                  "marginBottom": "0"}),
    ], className="mt-2")


# ----- Layer 3: math walkthrough -----

def build_math_walkthrough(statement_type: str, rows: list[dict]) -> html.Div:
    """Plain-text equations for the latest period — answers 'how derived'.
    Renders only equations whose inputs are present."""
    rows = _filter_substantive(statement_type, rows)
    if not rows:
        return html.Div()
    rows_sorted = sorted(rows, key=lambda r: r["period_end_date"], reverse=True)
    items = rows_sorted[0]["line_items"]
    period = rows_sorted[0]["period_end_date"]

    equations: list = []

    def fmt(v):
        return _fmt_signed_money(v)

    if statement_type == "income":
        rev = _first_match(items, _REVENUE_ALIASES)
        cogs = _first_match(items, ["Cost Of Revenue",
                                       "Reconciled Cost Of Revenue"])
        gp = _first_match(items, ["Gross Profit"])
        if rev and cogs is not None and gp is not None:
            gm = _common_size_pct(gp, rev)
            equations.append(
                f"Revenue {fmt(rev)} − Cost of Revenue {fmt(cogs)} = "
                f"Gross Profit {fmt(gp)}  ({_fmt_pct(gm) if gm else '—'} gross margin)"
            )
        oi = _first_match(items, ["Operating Income",
                                     "Total Operating Income As Reported"])
        tax = _first_match(items, ["Tax Provision"])
        ni = _first_match(items, ["Net Income",
                                     "Net Income Common Stockholders"])
        if oi is not None and ni is not None:
            om = _common_size_pct(oi, rev) if rev else None
            equations.append(
                f"Operating Income {fmt(oi)}"
                + (f" − Tax {fmt(tax)}" if tax is not None else "")
                + f" → Net Income {fmt(ni)}"
                + (f"  ({_fmt_pct(om)} operating margin)" if om else "")
            )
        ni_per = ni
        diluted_shares = _first_match(items, ["Diluted Average Shares"])
        diluted_eps = _first_match(items, ["Diluted EPS", "Basic EPS"])
        if ni_per and diluted_shares and diluted_eps:
            equations.append(
                f"Net Income {fmt(ni_per)} ÷ Diluted Shares {fmt(diluted_shares)}"
                f" = Diluted EPS {diluted_eps:.2f}"
            )

    elif statement_type == "balance":
        ta = _first_match(items, _TOTAL_ASSETS_ALIASES)
        tl = _first_match(items, ["Total Liabilities Net Minority Interest",
                                     "Total Liabilities"])
        te = _first_match(items, ["Stockholders Equity",
                                     "Total Equity Gross Minority Interest",
                                     "Total Equity"])
        if ta is not None and tl is not None and te is not None:
            identity_ok = abs((tl + te) - ta) <= max(abs(ta) * 0.01, 1.0)
            check = "  ✓ identity holds" if identity_ok else "  ⚠ off by >1%"
            equations.append(
                f"Total Assets {fmt(ta)} = Total Liabilities {fmt(tl)} + "
                f"Stockholders Equity {fmt(te)}{check}"
            )
        ca = _first_match(items, ["Current Assets"])
        cl = _first_match(items, ["Current Liabilities"])
        if ca is not None and cl and cl != 0:
            cr = ca / cl
            equations.append(
                f"Current Assets {fmt(ca)} ÷ Current Liabilities {fmt(cl)}"
                f" = Current Ratio {cr:.2f}×"
            )

    elif statement_type == "cashflow":
        ocf = _first_match(items, _OCF_ALIASES)
        capex = _first_match(items, ["Capital Expenditure", "Purchase Of PPE"])
        fcf_reported = _first_match(items, ["Free Cash Flow"])
        if ocf is not None and capex is not None:
            fcf = fcf_reported if fcf_reported is not None else ocf - abs(capex)
            derived_tag = "" if fcf_reported is not None else "  (derived)"
            equations.append(
                f"Operating Cash Flow {fmt(ocf)} − CapEx {fmt(abs(capex))}"
                f" = Free Cash Flow {fmt(fcf)}{derived_tag}"
            )
        div = _first_match(items, ["Cash Dividends Paid",
                                       "Common Stock Dividend Paid"])
        buyback = _first_match(items, ["Repurchase Of Capital Stock",
                                          "Common Stock Payments"])
        if div is not None or buyback is not None:
            parts = []
            if div is not None:
                parts.append(f"Dividends {fmt(abs(div))}")
            if buyback is not None:
                parts.append(f"Buybacks {fmt(abs(buyback))}")
            total = (abs(div) if div else 0) + (abs(buyback) if buyback else 0)
            equations.append("Cash returned to shareholders: "
                              + " + ".join(parts) + f" = {fmt(total)}")

    if not equations:
        return html.Div()
    return html.Div([
        html.Div("How the numbers fit together — latest period "
                  f"({period})",
                  style={"color": T.TEXT_MUTED, "fontSize": "0.72rem",
                          "fontWeight": "600", "textTransform": "uppercase",
                          "letterSpacing": "0.05em", "marginBottom": "4px"}),
        html.Ul([html.Li(eq, style={"color": T.TEXT,
                                       "fontSize": "0.82rem",
                                       "fontFamily": "ui-monospace, monospace",
                                       "lineHeight": "1.6"})
                  for eq in equations],
                  style={"paddingLeft": "18px", "marginBottom": "0"}),
    ], style={"background": T.CARD_BG_SOFT, "padding": "10px 14px",
                "borderRadius": "8px", "border": f"1px solid {T.BORDER}",
                "marginTop": "10px"})
