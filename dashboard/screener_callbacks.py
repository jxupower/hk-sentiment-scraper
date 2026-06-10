import sqlite3
import time
from statistics import mean, median

import plotly.graph_objects as go
from dash import Input, Output, State, callback_context
from dash.exceptions import PreventUpdate

from dashboard import theme as T


def _extract_clicked_bucket(click_bar, click_ann):
    """Pull the bucket name out of whichever click event just fired.
    Bar clicks expose it as `points[0].y`; annotation clicks as
    `annotation.text`. Uses callback_context.triggered to know which one."""
    triggered = callback_context.triggered or []
    prop_id = triggered[0]["prop_id"] if triggered else ""
    if prop_id.endswith("clickData") and click_bar:
        return click_bar["points"][0]["y"]
    if prop_id.endswith("clickAnnotationData") and click_ann:
        return click_ann["annotation"]["text"]
    return None


# Map of aggregation key -> (display name, function over list of (pe, market_cap) tuples).
# Cap-weighted uses the index methodology: Σ market_cap / Σ earnings,
# where earnings_i = market_cap_i / pe_i. This is what indices like the
# S&P 500's reported P/E actually use — gives heavier weight to mega-caps.
def _agg_median(items):
    return median([pe for pe, _ in items])


def _agg_mean(items):
    return mean([pe for pe, _ in items])


def _agg_cap_weighted(items):
    """Σ market_cap / Σ earnings (index P/E methodology). Drops items
    with missing/zero market_cap; returns None if the bucket has no
    usable cap data (caller falls back to median in that case)."""
    capped = [(pe, mc) for pe, mc in items if mc and mc > 0]
    if not capped:
        return None
    total_cap = sum(mc for _, mc in capped)
    total_earnings = sum(mc / pe for pe, mc in capped)
    if total_earnings <= 0:
        return None
    return total_cap / total_earnings


_AGG = {
    "median":       ("Median",       _agg_median),
    "mean":         ("Mean",         _agg_mean),
    "cap_weighted": ("Cap-weighted", _agg_cap_weighted),
}


def register_screener_callbacks(app, db_path: str):
    # Cell-click on the screener table's ticker column → jump to Research tab
    # and load that ticker. Uses the cross-tab-nav Store as the trigger, which
    # the Research-tab callbacks listen for.
    @app.callback(
        Output("main-tabs", "active_tab", allow_duplicate=True),
        Output("cross-tab-nav", "data", allow_duplicate=True),
        Input("screener-table", "active_cell"),
        State("screener-table", "data"),
        prevent_initial_call=True,
    )
    def jump_to_research_from_screener(active_cell, table_rows):
        if not active_cell or active_cell.get("column_id") != "ticker":
            raise PreventUpdate
        try:
            ticker = table_rows[active_cell["row"]]["ticker"]
        except (KeyError, IndexError, TypeError):
            raise PreventUpdate
        if not ticker:
            raise PreventUpdate
        # `ts` makes the Store payload unique per click — repeated clicks on the
        # same ticker still produce a new dict so render_report re-fires.
        return "tab-stock-research", {"ticker": ticker, "ts": int(time.time() * 1000)}

    # Click-to-filter on the sector P/E chart. Two click sources, same handler:
    #   * Bar click -> Dash fires `clickData`
    #   * Label click -> Dash fires `clickAnnotationData` (the y-axis labels
    #     are rendered as Plotly annotations with captureevents=True; default
    #     y-tick labels are hidden in _build_pe_chart_by_key).
    # Append-mode (locked): a second click on the same bucket is a no-op;
    # to remove, use the dropdown's × button. The downstream cascade
    # (update_screener) re-fires automatically because the dropdown value
    # is its Input.
    @app.callback(
        Output("screener-sector-filter", "value", allow_duplicate=True),
        Input("screener-sector-pe-chart", "clickData"),
        Input("screener-sector-pe-chart", "clickAnnotationData"),
        State("screener-sector-filter", "value"),
        prevent_initial_call=True,
    )
    def append_sector_filter_on_chart_click(click_bar, click_ann, current):
        clicked = _extract_clicked_bucket(click_bar, click_ann)
        if not clicked:
            raise PreventUpdate
        current = list(current or [])
        if clicked in current:
            raise PreventUpdate
        return current + [clicked]

    # Same pattern for the sub-sector chart. Self-contained (locked): does
    # NOT also fill the parent Sector dropdown; the sub-sector filter alone
    # narrows the table sufficiently.
    @app.callback(
        Output("screener-subsector-filter", "value", allow_duplicate=True),
        Input("screener-subsector-pe-chart", "clickData"),
        Input("screener-subsector-pe-chart", "clickAnnotationData"),
        State("screener-subsector-filter", "value"),
        prevent_initial_call=True,
    )
    def append_subsector_filter_on_chart_click(click_bar, click_ann, current):
        clicked = _extract_clicked_bucket(click_bar, click_ann)
        if not clicked:
            raise PreventUpdate
        current = list(current or [])
        if clicked in current:
            raise PreventUpdate
        return current + [clicked]

    @app.callback(
        Output("screener-table", "data"),
        Output("screener-stat-total", "children"),
        Output("screener-stat-with-data", "children"),
        Output("screener-stat-latest", "children"),
        Output("screener-row-count", "children"),
        Output("screener-sector-pe-chart", "figure"),
        Output("screener-subsector-pe-chart", "figure"),
        Output("screener-sector-pe-header", "children"),
        Output("screener-subsector-pe-header", "children"),
        Output("screener-sector-filter", "options"),
        Output("screener-subsector-filter", "options"),
        Input("screener-auto-refresh", "n_intervals"),
        Input("screener-refresh-btn", "n_clicks"),
        Input("screener-sector-filter", "value"),
        Input("screener-subsector-filter", "value"),
        Input("screener-tier-filter", "value"),
        Input("screener-completeness-filter", "value"),
        Input("screener-pe-aggregation", "value"),
    )
    def update_screener(_n, _clicks, sector_filter, subsector_filter,
                          tier_filter, min_completeness, pe_aggregation):
        rows = _query_latest(db_path)
        total_universe = _count_universe(db_path)
        latest_date = max((r["snapshot_date"] for r in rows), default="—")

        # Apply filters in Python (small dataset, simpler than SQL string construction)
        filtered = [r for r in rows
                    if (r.get("data_completeness") or 0) >= (min_completeness or 0)]
        if tier_filter == "watchlist":
            filtered = [r for r in filtered if r.get("is_watchlist") == 1]
        elif tier_filter == "universe":
            filtered = [r for r in filtered if not r.get("is_watchlist")]
        if sector_filter:
            chosen = set(sector_filter)
            filtered = [r for r in filtered if r.get("yf_sector") in chosen]
        if subsector_filter:
            chosen_sub = set(subsector_filter)
            filtered = [r for r in filtered if r.get("sub_sector") in chosen_sub]

        table_data = [_format_row(r) for r in filtered]

        # Dropdown options come from the full snapshot population, not filtered view
        sector_set = sorted({r["yf_sector"] for r in rows if r.get("yf_sector")})
        sector_options = [{"label": s, "value": s} for s in sector_set]
        subsector_set = sorted({r["sub_sector"] for r in rows if r.get("sub_sector")})
        subsector_options = [{"label": s, "value": s} for s in subsector_set]

        agg = pe_aggregation or "median"
        chart = _build_sector_pe_chart(filtered, aggregation=agg)
        subsector_chart = _build_subsector_pe_chart(filtered, aggregation=agg)
        agg_label = _AGG.get(agg, _AGG["median"])[0]
        sector_header = f"{agg_label} P/E by Sector"
        subsector_header = f"{agg_label} P/E by Sub-Sector"

        return (
            table_data,
            f"{total_universe:,}",
            f"{len(rows):,}",
            latest_date,
            f"{len(filtered):,} of {len(rows):,} matching filters",
            chart,
            subsector_chart,
            sector_header,
            subsector_header,
            sector_options,
            subsector_options,
        )


def _query_latest(db_path: str) -> list[dict]:
    """Latest snapshot per ticker, joined with securities for name + watchlist flag."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT f.ticker, f.snapshot_date, f.trailing_pe, f.forward_pe,
                   f.price_to_book, f.ev_to_ebitda, f.dividend_yield,
                   f.market_cap, f.beta, f.return_on_equity, f.debt_to_equity,
                   f.last_price, f.currency, f.data_completeness,
                   s.name, s.is_watchlist, s.watchlist_sector,
                   s.yf_sector, s.yf_industry, s.sub_sector, s.effective_sector
            FROM fundamentals_snapshots f
            INNER JOIN (
                SELECT ticker, MAX(snapshot_date) AS max_date
                FROM fundamentals_snapshots
                GROUP BY ticker
            ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
            INNER JOIN securities s ON f.ticker = s.ticker
            WHERE s.is_active = 1
            ORDER BY f.market_cap DESC NULLS LAST
        """).fetchall()
        return [dict(r) for r in rows]


def _count_universe(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM securities WHERE is_active = 1"
        ).fetchone()[0]


def _format_row(r: dict) -> dict:
    """Apply display formatting (rounding, ROE→%, market cap→billions, watchlist star)."""
    import math
    def rnd(v, digits=2):
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None  # defensive against stale string values like 'Infinity' in old rows
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, digits)

    market_cap = r.get("market_cap")
    roe = r.get("return_on_equity")
    completeness = r.get("data_completeness")

    return {
        "ticker": r.get("ticker"),
        "name": (r.get("name") or "")[:30],
        "yf_sector": r.get("yf_sector") or r.get("watchlist_sector") or "—",
        "sub_sector": r.get("sub_sector") or "—",
        "market_cap_b": rnd(market_cap / 1e9, 1) if market_cap else None,
        "trailing_pe": rnd(r.get("trailing_pe"), 1),
        "forward_pe": rnd(r.get("forward_pe"), 1),
        "price_to_book": rnd(r.get("price_to_book"), 2),
        "ev_to_ebitda": rnd(r.get("ev_to_ebitda"), 1),
        "dividend_yield": rnd(r.get("dividend_yield"), 2),
        "return_on_equity_pct": rnd(roe * 100, 1) if roe is not None else None,
        "debt_to_equity": rnd(r.get("debt_to_equity"), 1),
        "beta": rnd(r.get("beta"), 2),
        "completeness_pct": rnd((completeness or 0) * 100, 0),
        "watchlist_flag": "★" if r.get("is_watchlist") else "",
    }


def _build_sector_pe_chart(rows: list[dict], aggregation: str = "median") -> go.Figure:
    """Trailing P/E per sector (only sectors with ≥3 tickers, P/E in 0-200 range)."""
    return _build_pe_chart_by_key(
        rows, aggregation=aggregation,
        bucket_key=lambda r: r.get("yf_sector") or r.get("watchlist_sector"),
        empty_message=("Need ≥3 tickers per sector with valid P/E. "
                       "Run 'fundamentals refresh --tickers ALL' to populate."),
    )


def _build_subsector_pe_chart(rows: list[dict], aggregation: str = "median") -> go.Figure:
    """Trailing P/E per SUB-SECTOR — finer-grained companion to the sector
    chart above. Same filters (P/E in (0, 200], ≥3 tickers per bucket), same
    styling, same aggregation as the sector chart (driven by a single toggle).
    Tickers without a sub_sector assignment (yfinance-metadata-NULL) dropped."""
    return _build_pe_chart_by_key(
        rows, aggregation=aggregation,
        bucket_key=lambda r: r.get("sub_sector"),
        empty_message=("Need ≥3 tickers per sub-sector with valid P/E. "
                       "Filter to a parent sector or backfill yfinance metadata."),
    )


def _build_pe_chart_by_key(rows: list[dict], bucket_key, empty_message: str,
                             aggregation: str = "median") -> go.Figure:
    """Shared horizontal-bar P/E chart builder. `bucket_key` is a callable
    taking a row and returning the string label to bucket on (or falsy to
    skip). `aggregation` is one of "median" / "mean" / "cap_weighted" —
    see the _AGG dict at the top of the module. Each bucket stores
    (pe, market_cap) tuples so cap-weighted aggregation has what it needs;
    median/mean simply ignore the second tuple element."""
    agg_label, agg_fn = _AGG.get(aggregation, _AGG["median"])

    by_bucket: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        key = bucket_key(r)
        pe = r.get("trailing_pe")
        if not key or pe is None or pe <= 0 or pe > 200:
            continue
        mc = r.get("market_cap") or 0.0
        by_bucket.setdefault(key, []).append((float(pe), float(mc)))

    eligible = []
    for s, items in by_bucket.items():
        if len(items) < 3:
            continue
        agg_value = agg_fn(items)
        if agg_value is None:
            # Cap-weighted may fall back to None if no tickers in the bucket
            # have market_cap data; quietly degrade to median so the bar
            # still renders rather than disappearing.
            agg_value = _agg_median(items)
        eligible.append((s, agg_value))
    eligible.sort(key=lambda x: x[1])

    if not eligible:
        fig = go.Figure()
        fig.add_annotation(text=empty_message,
                           xref="paper", yref="paper", x=0.5, y=0.5,
                           showarrow=False, font=dict(size=13, color="#90a4ae"))
        fig.update_layout(_dark_layout(""), height=180)
        return fig

    buckets = [s for s, _ in eligible]
    values = [v for _, v in eligible]
    counts = [len(by_bucket[s]) for s in buckets]

    # Replace the default y-axis tick labels with clickable annotations.
    # Plotly tick labels don't fire clickData; annotations with captureevents=True
    # fire clickAnnotationData, which Dash exposes as a `dcc.Graph` prop. Same
    # text and styling as default ticks so the visual is unchanged.
    label_annotations = [
        dict(
            x=0, y=bucket,
            xref="x", yref="y",
            text=bucket,
            showarrow=False,
            xanchor="right", yanchor="middle",
            xshift=-8,
            font=dict(size=11, color=T.TEXT),
            captureevents=True,
            hovertext=f"Click to filter to: {bucket}",
        )
        for bucket in buckets
    ]

    fig = go.Figure(go.Bar(
        x=values, y=buckets, orientation="h",
        marker_color="#1976d2",
        text=[f"{v:.1f} (n={c})" for v, c in zip(values, counts)],
        textposition="outside",
        hovertemplate=f"<b>%{{y}}</b><br>{agg_label} P/E: %{{x:.1f}}<br>Tickers: %{{text}}<extra></extra>",
    ))
    fig.update_layout(_dark_layout(""),
                      xaxis_title=f"{agg_label} Trailing P/E",
                      yaxis=dict(showticklabels=False),  # hidden — annotations above are the visible (clickable) labels
                      annotations=label_annotations,
                      height=max(220, len(buckets) * 28 + 80),
                      margin=dict(l=220, r=80, t=20, b=40))
    return fig


def _dark_layout(title: str) -> dict:
    base = T.chart_layout(title=title, height=240)
    base["legend"] = dict(bgcolor=T.CARD_BG, bordercolor=T.BORDER, borderwidth=1,
                          font=dict(color=T.TEXT))
    return base
