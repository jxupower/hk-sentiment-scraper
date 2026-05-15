from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

CARD_STYLE = {"background": "#1a1a2e", "border": "1px solid #37474f"}

REC_COLORS = {
    "STRONG BUY":   "#00c853",
    "BUY":          "#69f0ae",
    "HOLD":         "#90a4ae",
    "SELL":         "#ff8a65",
    "STRONG SELL":  "#d50000",
    "—":            "#37474f",
}


def build_recommendations_tab() -> html.Div:
    return html.Div([
        dcc.Interval(id="rec-auto-refresh", interval=300_000, n_intervals=0),

        # Diagnostic banner — surfaces when data is thin
        dbc.Alert(id="rec-diagnostic-banner", color="warning", className="small mb-3",
                  is_open=False, dismissable=True),

        # Stats strip
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Span("Ranked tickers: ", className="text-muted small me-1"),
                        html.Span(id="rec-stat-total", className="text-light fw-bold"),
                    ], width=2),
                    dbc.Col(id="rec-stat-breakdown", width=8),
                    dbc.Col([
                        dbc.Button("Recompute", id="rec-refresh-btn", color="primary",
                                   size="sm", className="float-end"),
                    ], width=2),
                ], align="center"),
            ], style={"padding": "10px 16px"}),
        ], style=CARD_STYLE, className="mb-3"),

        # Controls row: weight slider + sentiment window + filters
        dbc.Card([
            dbc.CardHeader("Controls", className="fw-bold small"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label([
                            "Valuation vs Sentiment weight  ",
                            html.Span(id="rec-weight-display",
                                      className="text-info small fw-bold"),
                        ], className="text-muted small mb-1"),
                        dcc.Slider(
                            id="rec-weight-slider",
                            min=0, max=100, step=10, value=60,
                            marks={
                                0:   {"label": "100% sent", "style": {"color": "#90a4ae"}},
                                50:  {"label": "50/50",     "style": {"color": "#90a4ae"}},
                                100: {"label": "100% val",  "style": {"color": "#90a4ae"}},
                            },
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                    ], width=4),
                    dbc.Col([
                        html.Label("Sentiment window (days)", className="text-muted small mb-1"),
                        dcc.Slider(
                            id="rec-window-slider",
                            min=1, max=30, step=1, value=7,
                            marks={1: "1d", 7: "7d", 14: "14d", 30: "30d"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                    ], width=3),
                    dbc.Col([
                        html.Label("Regime", className="text-muted small mb-1"),
                        dcc.Checklist(
                            id="rec-regime-filter",
                            options=[
                                {"label": " Deep (watchlist)", "value": "deep"},
                                {"label": " Covered (universe + sentiment)", "value": "covered"},
                                {"label": " Uncovered (fundamentals only)", "value": "uncovered"},
                            ],
                            value=["deep", "covered", "uncovered"],
                            labelClassName="text-light me-3 small d-block",
                        ),
                    ], width=3),
                    dbc.Col([
                        html.Label("Recommendation", className="text-muted small mb-1"),
                        dcc.Checklist(
                            id="rec-tag-filter",
                            options=[
                                {"label": " STRONG BUY",  "value": "STRONG BUY"},
                                {"label": " BUY",         "value": "BUY"},
                                {"label": " HOLD",        "value": "HOLD"},
                                {"label": " SELL",        "value": "SELL"},
                                {"label": " STRONG SELL", "value": "STRONG SELL"},
                            ],
                            value=["STRONG BUY", "BUY", "SELL", "STRONG SELL"],
                            labelClassName="text-light me-3 small d-block",
                        ),
                    ], width=2),
                ]),
            ]),
        ], style=CARD_STYLE, className="mb-3"),

        # Composite-score distribution chart
        dbc.Card([
            dbc.CardHeader("Composite Score Distribution", className="fw-bold small"),
            dbc.CardBody([
                dcc.Graph(id="rec-distribution-chart",
                          config={"displayModeBar": False}, figure={}),
            ]),
        ], style=CARD_STYLE, className="mb-3"),

        # Ranked table
        dbc.Card([
            dbc.CardHeader([
                html.Span("Ranked Recommendations", className="fw-bold small me-2"),
                html.Span(id="rec-row-count", className="text-muted small"),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id="rec-table",
                    columns=[
                        {"name": "Ticker",      "id": "ticker"},
                        {"name": "Name",        "id": "name"},
                        {"name": "Sector",      "id": "sector"},
                        {"name": "Regime",      "id": "regime"},
                        {"name": "Recommendation", "id": "recommendation"},
                        {"name": "Composite",   "id": "composite_score", "type": "numeric"},
                        {"name": "Val Z",       "id": "valuation_z", "type": "numeric"},
                        {"name": "Sent Z",      "id": "sentiment_z", "type": "numeric"},
                        {"name": "Articles 7d", "id": "article_count_7d", "type": "numeric"},
                        {"name": "P/E",         "id": "trailing_pe", "type": "numeric"},
                        {"name": "P/B",         "id": "price_to_book", "type": "numeric"},
                        {"name": "Div Yld %",   "id": "dividend_yield", "type": "numeric"},
                        {"name": "Mkt Cap (B)", "id": "market_cap_b", "type": "numeric"},
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
                        {"if": {"column_id": "sector"}, "textAlign": "left", "fontFamily": "inherit"},
                        {"if": {"column_id": "regime"}, "textAlign": "center"},
                        {"if": {"column_id": "recommendation"}, "textAlign": "center", "fontWeight": "bold"},
                    ],
                    style_header={
                        "backgroundColor": "#1a1a2e", "color": "#90caf9",
                        "fontWeight": "bold", "fontSize": "0.75rem",
                    },
                    style_data_conditional=[
                        {"if": {"filter_query": f'{{recommendation}} = "{rec}"',
                                "column_id": "recommendation"},
                         "color": color, "fontWeight": "bold"}
                        for rec, color in REC_COLORS.items()
                    ] + [
                        {"if": {"filter_query": '{regime} = "deep"', "column_id": "regime"},
                         "color": "#ffd600"},
                        {"if": {"filter_query": '{regime} = "covered"', "column_id": "regime"},
                         "color": "#90caf9"},
                        {"if": {"filter_query": '{regime} = "uncovered"', "column_id": "regime"},
                         "color": "#90a4ae"},
                    ],
                    style_filter={"backgroundColor": "#0f1a2e", "color": "#eceff1"},
                ),
            ]),
        ], style=CARD_STYLE),
    ])
