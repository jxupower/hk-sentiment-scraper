"""Chart + table builders for the Stock Research Section 3b financial-statements UI.

Kept separate from stock_research_callbacks.py (which is already large) so the
~200 lines of formatting logic live in one focused module.

Public API:
    build_statement_chart(statement_type, rows) -> go.Figure
    build_statement_table(statement_type, rows) -> dash_table.DataTable
    build_earnings_chart(income_rows) -> go.Figure
    build_earnings_table(income_rows) -> dash_table.DataTable
"""
from typing import Optional

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
