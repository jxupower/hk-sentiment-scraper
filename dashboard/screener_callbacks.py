import sqlite3
from statistics import median

import plotly.graph_objects as go
from dash import Input, Output


def register_screener_callbacks(app, db_path: str):
    @app.callback(
        Output("screener-table", "data"),
        Output("screener-stat-total", "children"),
        Output("screener-stat-with-data", "children"),
        Output("screener-stat-latest", "children"),
        Output("screener-row-count", "children"),
        Output("screener-sector-pe-chart", "figure"),
        Output("screener-sector-filter", "options"),
        Input("screener-auto-refresh", "n_intervals"),
        Input("screener-refresh-btn", "n_clicks"),
        Input("screener-sector-filter", "value"),
        Input("screener-tier-filter", "value"),
        Input("screener-completeness-filter", "value"),
    )
    def update_screener(_n, _clicks, sector_filter, tier_filter, min_completeness):
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

        table_data = [_format_row(r) for r in filtered]

        # Sector dropdown options come from the full snapshot population, not the filtered view
        sector_set = sorted({r["yf_sector"] for r in rows if r.get("yf_sector")})
        sector_options = [{"label": s, "value": s} for s in sector_set]

        chart = _build_sector_pe_chart(filtered)

        return (
            table_data,
            f"{total_universe:,}",
            f"{len(rows):,}",
            latest_date,
            f"{len(filtered):,} of {len(rows):,} matching filters",
            chart,
            sector_options,
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
                   s.yf_sector, s.yf_industry
            FROM fundamentals_snapshots f
            INNER JOIN (
                SELECT ticker, MAX(snapshot_date) AS max_date
                FROM fundamentals_snapshots
                GROUP BY ticker
            ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
            LEFT JOIN securities s ON f.ticker = s.ticker
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
    def rnd(v, digits=2):
        return round(v, digits) if v is not None else None

    market_cap = r.get("market_cap")
    roe = r.get("return_on_equity")
    completeness = r.get("data_completeness")

    return {
        "ticker": r.get("ticker"),
        "name": (r.get("name") or "")[:30],
        "yf_sector": r.get("yf_sector") or r.get("watchlist_sector") or "—",
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


def _build_sector_pe_chart(rows: list[dict]) -> go.Figure:
    """Median trailing P/E per sector (only sectors with ≥3 tickers, P/E in 0-200 range)."""
    by_sector: dict[str, list[float]] = {}
    for r in rows:
        sector = r.get("yf_sector") or r.get("watchlist_sector")
        pe = r.get("trailing_pe")
        if not sector or pe is None or pe <= 0 or pe > 200:
            continue
        by_sector.setdefault(sector, []).append(float(pe))

    eligible = [(s, median(v)) for s, v in by_sector.items() if len(v) >= 3]
    eligible.sort(key=lambda x: x[1])

    if not eligible:
        fig = go.Figure()
        fig.add_annotation(text="Need ≥3 tickers per sector with valid P/E. "
                                "Run 'fundamentals refresh --tickers ALL' to populate.",
                           xref="paper", yref="paper", x=0.5, y=0.5,
                           showarrow=False, font=dict(size=13, color="#90a4ae"))
        fig.update_layout(_dark_layout(""), height=180)
        return fig

    sectors = [s for s, _ in eligible]
    medians = [m for _, m in eligible]
    counts = [len(by_sector[s]) for s in sectors]

    fig = go.Figure(go.Bar(
        x=medians, y=sectors, orientation="h",
        marker_color="#1976d2",
        text=[f"{m:.1f} (n={c})" for m, c in zip(medians, counts)],
        textposition="outside",
        hovertemplate="<b>%{y}</b><br>Median P/E: %{x:.1f}<br>Tickers: %{text}<extra></extra>",
    ))
    fig.update_layout(_dark_layout(""),
                      xaxis_title="Median Trailing P/E",
                      height=max(220, len(sectors) * 28 + 80),
                      margin=dict(l=180, r=80, t=20, b=40))
    return fig


def _dark_layout(title: str) -> dict:
    return dict(
        title={"text": title, "font": {"color": "#eceff1", "size": 13}},
        paper_bgcolor="#1a1a2e",
        plot_bgcolor="#16213e",
        font=dict(color="#eceff1", size=11),
        legend=dict(bgcolor="#1a1a2e", bordercolor="#37474f", borderwidth=1),
    )
