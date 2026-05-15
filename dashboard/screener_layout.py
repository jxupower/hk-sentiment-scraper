from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

CARD_STYLE = {"background": "#1a1a2e", "border": "1px solid #37474f"}


def build_screener_tab() -> html.Div:
    return html.Div([
        dcc.Interval(id="screener-auto-refresh", interval=300_000, n_intervals=0),

        # Header strip with high-level stats
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Span("Universe size: ", className="text-muted small me-1"),
                        html.Span(id="screener-stat-total", className="text-light fw-bold"),
                    ], width=3),
                    dbc.Col([
                        html.Span("With fundamentals: ", className="text-muted small me-1"),
                        html.Span(id="screener-stat-with-data", className="text-info fw-bold"),
                    ], width=3),
                    dbc.Col([
                        html.Span("Latest snapshot: ", className="text-muted small me-1"),
                        html.Span(id="screener-stat-latest", className="text-warning"),
                    ], width=3),
                    dbc.Col([
                        dbc.Button("Refresh", id="screener-refresh-btn", color="primary",
                                   size="sm", className="float-end"),
                    ], width=3),
                ], align="center"),
            ], style={"padding": "10px 16px"}),
        ], style=CARD_STYLE, className="mb-3"),

        # Filters row
        dbc.Card([
            dbc.CardHeader("Filters", className="fw-bold small"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Sector", className="text-muted small mb-1"),
                        dcc.Dropdown(id="screener-sector-filter", multi=True,
                                     placeholder="All sectors",
                                     style={"background": "#263238"}),
                    ], width=4),
                    dbc.Col([
                        html.Label("View", className="text-muted small mb-1"),
                        dcc.RadioItems(
                            id="screener-tier-filter",
                            options=[
                                {"label": " All universe", "value": "all"},
                                {"label": " Watchlist only", "value": "watchlist"},
                                {"label": " Universe only (non-watchlist)", "value": "universe"},
                            ],
                            value="all",
                            labelClassName="text-light me-3 small",
                        ),
                    ], width=4),
                    dbc.Col([
                        html.Label("Min data completeness", className="text-muted small mb-1"),
                        dcc.Slider(
                            id="screener-completeness-filter",
                            min=0, max=1, step=0.1, value=0.5,
                            marks={i / 10: f"{i*10}%" for i in range(0, 11, 2)},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                    ], width=4),
                ]),
            ]),
        ], style=CARD_STYLE, className="mb-3"),

        # Sector summary chart (median P/E per sector)
        dbc.Card([
            dbc.CardHeader("Median P/E by Sector", className="fw-bold small"),
            dbc.CardBody([
                dcc.Graph(id="screener-sector-pe-chart",
                          config={"displayModeBar": False}, figure={}),
            ]),
        ], style=CARD_STYLE, className="mb-3"),

        # The big table
        dbc.Card([
            dbc.CardHeader([
                html.Span("Tickers", className="fw-bold small me-2"),
                html.Span(id="screener-row-count", className="text-muted small"),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id="screener-table",
                    columns=[
                        {"name": "Ticker", "id": "ticker"},
                        {"name": "Name", "id": "name"},
                        {"name": "Sector", "id": "yf_sector"},
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
                    style_cell={
                        "backgroundColor": "#16213e", "color": "#eceff1",
                        "fontSize": "0.8rem", "padding": "6px 8px",
                        "fontFamily": "monospace", "textAlign": "right",
                    },
                    style_cell_conditional=[
                        {"if": {"column_id": "ticker"}, "textAlign": "left"},
                        {"if": {"column_id": "name"}, "textAlign": "left", "fontFamily": "inherit"},
                        {"if": {"column_id": "yf_sector"}, "textAlign": "left", "fontFamily": "inherit"},
                        {"if": {"column_id": "watchlist_flag"}, "textAlign": "center"},
                    ],
                    style_header={
                        "backgroundColor": "#1a1a2e", "color": "#90caf9",
                        "fontWeight": "bold", "fontSize": "0.75rem",
                    },
                    style_data_conditional=[
                        {"if": {"filter_query": "{watchlist_flag} = '★'"},
                         "backgroundColor": "#1f2942"},
                    ],
                    style_filter={"backgroundColor": "#0f1a2e", "color": "#eceff1"},
                ),
            ]),
        ], style=CARD_STYLE),
    ])
