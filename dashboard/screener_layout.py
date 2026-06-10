from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from dashboard import theme as T


def _stat_block(label: str, value_id: str, value_color: str = None):
    """Hero number with small uppercase label above."""
    return html.Div([
        html.Div(label, className="stat-label"),
        html.Div(id=value_id, className="hero-number",
                  style={"color": value_color} if value_color else {}),
    ])


def build_screener_tab() -> html.Div:
    return html.Div([
        dcc.Interval(id="screener-auto-refresh", interval=300_000, n_intervals=0),

        # Header strip — 3 hero stats + action button
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(_stat_block("Universe size", "screener-stat-total"), width=3),
                    dbc.Col(_stat_block("With fundamentals", "screener-stat-with-data",
                                          value_color=T.PRIMARY), width=3),
                    dbc.Col(_stat_block("Latest snapshot", "screener-stat-latest"), width=4),
                    dbc.Col([
                        # Two refresh actions side-by-side:
                        # * "Refresh" re-reads the cached DB (fast, no API call)
                        # * "Refresh prices now" pulls fresh bars from yfinance
                        #   in a background thread (~5-10 min on full universe)
                        dbc.Button("Refresh", id="screener-refresh-btn",
                                    color="primary", size="sm",
                                    className="float-end mt-2"),
                        dbc.Button("Refresh prices now",
                                    id="screener-refresh-prices-btn",
                                    color="warning", outline=True, size="sm",
                                    className="float-end mt-2 me-2"),
                        html.Div(id="screener-refresh-prices-status",
                                  className="text-end small text-muted mt-1",
                                  style={"clear": "both", "fontSize": "0.72rem"}),
                    ], width=2),
                ], align="center"),
            ], style={"padding": "20px 24px"}),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Filters row
        dbc.Card([
            dbc.CardHeader(
                dbc.Row([
                    dbc.Col(html.Span("Filters", className="fw-bold"),
                            width="auto"),
                    dbc.Col(
                        dbc.Button("Clear filters",
                                    id="screener-clear-filters-btn",
                                    color="secondary", outline=True, size="sm"),
                        className="text-end",
                    ),
                ], align="center", className="g-0"),
            ),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Sector", className="stat-label mb-2"),
                        dcc.Dropdown(id="screener-sector-filter", multi=True,
                                     placeholder="All sectors"),
                    ], xs=12, md=3),
                    dbc.Col([
                        html.Label("Sub-sector", className="stat-label mb-2"),
                        dcc.Dropdown(id="screener-subsector-filter", multi=True,
                                     placeholder="All sub-sectors"),
                    ], xs=12, md=3),
                    dbc.Col([
                        html.Label("View", className="stat-label mb-2"),
                        dcc.RadioItems(
                            id="screener-tier-filter",
                            options=[
                                {"label": " All universe", "value": "all"},
                                {"label": " Watchlist only", "value": "watchlist"},
                                {"label": " Universe only (non-watchlist)", "value": "universe"},
                            ],
                            value="all",
                            labelClassName="me-3",
                            style={"fontSize": "0.9rem", "color": T.TEXT},
                        ),
                    ], xs=12, md=3),
                    dbc.Col([
                        html.Label("Min data completeness", className="stat-label mb-2"),
                        dcc.Slider(
                            id="screener-completeness-filter",
                            min=0, max=1, step=0.1, value=0.5,
                            marks={i / 10: f"{i*10}%" for i in range(0, 11, 2)},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                    ], xs=12, md=3),
                ], className="g-3"),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # P/E aggregation toggle — single control feeds both charts below.
        # Median is the default (robust to outliers). Mean is a simple
        # arithmetic average. Cap-weighted uses the index methodology
        # (Σ market_cap / Σ earnings), giving more weight to mega-caps.
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("P/E aggregation",
                                    className="stat-label mb-1"),
                        dbc.RadioItems(
                            id="screener-pe-aggregation",
                            options=[
                                {"label": "Median", "value": "median"},
                                {"label": "Mean", "value": "mean"},
                                {"label": "Cap-weighted", "value": "cap_weighted"},
                            ],
                            value="median",
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12),
                ]),
            ], style={"padding": "12px 16px"}),
        ], style=T.CARD_STYLE, className="mb-2"),

        # Sector summary chart — aggregation method comes from the toggle above
        dbc.Card([
            dbc.CardHeader(id="screener-sector-pe-header",
                            children="Median P/E by Sector"),
            dbc.CardBody([
                dcc.Graph(id="screener-sector-pe-chart",
                          config={"displayModeBar": False}, figure={}),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Sub-sector summary chart — same statistic at finer granularity.
        # Auto-narrows when the user filters to a parent sector (e.g. picking
        # Technology shows the 8 Tech sub-sector medians only).
        dbc.Card([
            dbc.CardHeader(id="screener-subsector-pe-header",
                            children="Median P/E by Sub-Sector"),
            dbc.CardBody([
                dcc.Graph(id="screener-subsector-pe-chart",
                          config={"displayModeBar": False}, figure={}),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # The big table
        dbc.Card([
            dbc.CardHeader([
                html.Span("Tickers", style={"fontWeight": "600", "marginRight": "10px"}),
                html.Span(id="screener-row-count",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.85rem"}),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id="screener-table",
                    columns=[
                        {"name": "Ticker", "id": "ticker"},
                        {"name": "Name", "id": "name"},
                        {"name": "Sector", "id": "yf_sector"},
                        {"name": "Sub-sector", "id": "sub_sector"},
                        {"name": "Mkt Cap (B HKD)", "id": "market_cap_b", "type": "numeric"},
                        {"name": "P/E", "id": "trailing_pe", "type": "numeric"},
                        {"name": "Fwd P/E", "id": "forward_pe", "type": "numeric"},
                        {"name": "P/B", "id": "price_to_book", "type": "numeric"},
                        {"name": "EV/EBITDA", "id": "ev_to_ebitda", "type": "numeric"},
                        {"name": "Div Yield (%)", "id": "dividend_yield", "type": "numeric"},
                        {"name": "ROE (%)", "id": "return_on_equity_pct", "type": "numeric"},
                        {"name": "D/E (%)", "id": "debt_to_equity", "type": "numeric"},
                        {"name": "Beta", "id": "beta", "type": "numeric"},
                        {"name": "Completeness", "id": "completeness_pct", "type": "numeric"},
                        {"name": "WL", "id": "watchlist_flag"},
                    ],
                    data=[],
                    page_size=25,
                    sort_action="native",
                    filter_action="native",
                    style_cell=T.DATATABLE_CELL,
                    style_cell_conditional=[
                        {"if": {"column_id": "ticker"}, "textAlign": "left",
                         "fontWeight": "600", "color": T.PRIMARY,
                         "cursor": "pointer", "textDecoration": "underline"},
                        {"if": {"column_id": "name"}, "textAlign": "left",
                         "fontFamily": "Inter, sans-serif"},
                        {"if": {"column_id": "yf_sector"}, "textAlign": "left",
                         "fontFamily": "Inter, sans-serif", "color": T.TEXT_MUTED},
                        {"if": {"column_id": "sub_sector"}, "textAlign": "left",
                         "fontFamily": "Inter, sans-serif", "color": T.TEXT_MUTED,
                         "fontSize": "0.85rem"},
                        {"if": {"column_id": "watchlist_flag"}, "textAlign": "center"},
                    ],
                    style_header=T.DATATABLE_HEADER,
                    style_data_conditional=[
                        {"if": {"filter_query": "{watchlist_flag} = '★'"},
                         "backgroundColor": T.PRIMARY_SOFT},
                    ],
                    style_filter=T.DATATABLE_FILTER,
                ),
            ]),
        ], style=T.CARD_STYLE),
    ])
