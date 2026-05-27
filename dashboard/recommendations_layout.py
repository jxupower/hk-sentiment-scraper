from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

CARD_STYLE = {"background": "#1a1a2e", "border": "1px solid #37474f"}

FLAG_COLORS = {"high": "#d50000", "medium": "#ff8a65", "low": "#90caf9"}

INPUT_STYLE = {
    "textAlign": "right", "background": "#263238", "color": "#eceff1",
    "border": "1px solid #37474f",
}
INPUT_SUFFIX_STYLE = {
    "background": "#1a1a2e", "color": "#90a4ae", "border": "1px solid #37474f",
}


def build_recommendations_tab() -> html.Div:
    """The Discovery tab — multi-factor percentile-rank candidates for research."""
    return html.Div([
        dcc.Interval(id="rec-auto-refresh", interval=300_000, n_intervals=0),

        # Caveat banner — surfaces what this is and isn't
        dbc.Alert([
            html.Strong("Discovery, not recommendations. "),
            "Candidates for further research, not buy/sell advice. ",
            "Scores are sector-relative percentile ranks (0=worst, 100=best). ",
            "A high composite rank means 'looks attractive across these factors vs sector peers' — ",
            "it does NOT account for business catalysts, governance, capital structure, or qualitative risks. ",
            "Flagged tickers carry known macro/regulatory issues that may distort valuation; ",
            "they remain visible but should be researched extra-carefully.",
        ], color="info", className="small mb-3", dismissable=True),

        # Dynamic diagnostic banner (engine surfaces data-depth notes here)
        dbc.Alert(id="rec-diagnostic-banner", color="warning", className="small mb-3",
                  is_open=False, dismissable=True),

        # Stats strip
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Span("Scorable: ", className="text-muted small me-1"),
                        html.Span(id="rec-stat-scorable", className="text-light fw-bold"),
                        html.Span("  ·  Disqualified: ", className="text-muted small ms-2 me-1"),
                        html.Span(id="rec-stat-disqualified", className="text-warning fw-bold"),
                        html.Span("  ·  Flagged: ", className="text-muted small ms-2 me-1"),
                        html.Span(id="rec-stat-flagged", className="text-danger fw-bold"),
                    ], width=8),
                    dbc.Col([
                        dbc.Button("Recompute", id="rec-refresh-btn", color="primary",
                                   size="sm", className="float-end"),
                    ], width=4),
                ], align="center"),
            ], style={"padding": "10px 16px"}),
        ], style=CARD_STYLE, className="mb-3"),

        # Factor weight controls — 4 number inputs, normalized in the engine
        dbc.Card([
            dbc.CardHeader([
                html.Span("Factor Weights", className="fw-bold small me-2"),
                html.Span(id="rec-weights-normalized", className="text-info small fw-bold"),
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Value (cheap)", className="text-muted small mb-1"),
                        dbc.InputGroup([
                            dbc.Input(id="rec-weight-value", type="number",
                                      min=0, max=100, step=5, value=30,
                                      size="sm", style=INPUT_STYLE),
                            dbc.InputGroupText("%", className="small",
                                               style=INPUT_SUFFIX_STYLE),
                        ], size="sm"),
                    ], width=3),
                    dbc.Col([
                        html.Label("Quality (ROE, low debt)",
                                   className="text-muted small mb-1"),
                        dbc.InputGroup([
                            dbc.Input(id="rec-weight-quality", type="number",
                                      min=0, max=100, step=5, value=30,
                                      size="sm", style=INPUT_STYLE),
                            dbc.InputGroupText("%", className="small",
                                               style=INPUT_SUFFIX_STYLE),
                        ], size="sm"),
                    ], width=3),
                    dbc.Col([
                        html.Label("Growth (earnings, revenue)",
                                   className="text-muted small mb-1"),
                        dbc.InputGroup([
                            dbc.Input(id="rec-weight-growth", type="number",
                                      min=0, max=100, step=5, value=20,
                                      size="sm", style=INPUT_STYLE),
                            dbc.InputGroupText("%", className="small",
                                               style=INPUT_SUFFIX_STYLE),
                        ], size="sm"),
                    ], width=3),
                    dbc.Col([
                        html.Label("Sentiment (news mood)",
                                   className="text-muted small mb-1"),
                        dbc.InputGroup([
                            dbc.Input(id="rec-weight-sentiment", type="number",
                                      min=0, max=100, step=5, value=20,
                                      size="sm", style=INPUT_STYLE),
                            dbc.InputGroupText("%", className="small",
                                               style=INPUT_SUFFIX_STYLE),
                        ], size="sm"),
                    ], width=3),
                ], className="g-2"),
                html.Div([
                    html.Span("Sentiment window: ", className="text-muted small me-1"),
                    dcc.Slider(
                        id="rec-window-slider",
                        min=1, max=30, step=1, value=7,
                        marks={1: "1d", 7: "7d", 14: "14d", 30: "30d"},
                        tooltip={"placement": "bottom", "always_visible": False},
                    ),
                ], className="mt-3"),
                html.Div([
                    html.Label("Filters", className="text-muted small mb-1 mt-2"),
                    dbc.Row([
                        dbc.Col([
                            html.Label("Min composite percentile",
                                       className="text-muted small mb-1"),
                            dcc.Slider(
                                id="rec-min-composite-filter",
                                min=0, max=100, step=5, value=0,
                                marks={0: "0", 50: "50", 75: "75", 100: "100"},
                                tooltip={"placement": "bottom", "always_visible": False},
                            ),
                        ], width=4),
                        dbc.Col([
                            html.Label("Show", className="text-muted small mb-1"),
                            dcc.Checklist(
                                id="rec-show-filter",
                                options=[
                                    {"label": " Watchlist only", "value": "watchlist"},
                                    {"label": " Include flagged",
                                     "value": "include_flagged"},
                                    {"label": " Include disqualified (educational)",
                                     "value": "include_dq"},
                                ],
                                value=["include_flagged"],
                                labelClassName="text-light me-3 small d-block",
                            ),
                        ], width=4),
                        dbc.Col([
                            html.Label("Sector", className="text-muted small mb-1"),
                            dcc.Dropdown(id="rec-sector-filter", multi=True,
                                         placeholder="All sectors",
                                         style={"background": "#263238"}),
                        ], width=4),
                    ]),
                ]),
            ]),
        ], style=CARD_STYLE, className="mb-3"),

        # Composite distribution chart
        dbc.Card([
            dbc.CardHeader("Composite Percentile Distribution",
                          className="fw-bold small"),
            dbc.CardBody([
                dcc.Graph(id="rec-distribution-chart",
                          config={"displayModeBar": False}, figure={}),
            ]),
        ], style=CARD_STYLE, className="mb-3"),

        # Ranked table
        dbc.Card([
            dbc.CardHeader([
                html.Span("Discovery Candidates", className="fw-bold small me-2"),
                html.Span(id="rec-row-count", className="text-muted small"),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id="rec-table",
                    columns=[
                        {"name": "Ticker",        "id": "ticker"},
                        {"name": "Name",          "id": "name"},
                        {"name": "Sector",        "id": "sector"},
                        {"name": "Composite %",   "id": "composite_pctile",
                         "type": "numeric"},
                        {"name": "Value %",       "id": "value_pctile",
                         "type": "numeric"},
                        {"name": "Quality %",     "id": "quality_pctile",
                         "type": "numeric"},
                        {"name": "Growth %",      "id": "growth_pctile",
                         "type": "numeric"},
                        {"name": "Sentiment %",   "id": "sentiment_pctile",
                         "type": "numeric"},
                        {"name": "Articles",      "id": "article_count",
                         "type": "numeric"},
                        {"name": "P/E",           "id": "trailing_pe",
                         "type": "numeric"},
                        {"name": "ROE %",         "id": "roe_display",
                         "type": "numeric"},
                        {"name": "Earn Growth %", "id": "earn_growth_display",
                         "type": "numeric"},
                        {"name": "Mkt Cap (B)",   "id": "market_cap_b",
                         "type": "numeric"},
                        {"name": "Status",        "id": "status_badge"},
                    ],
                    data=[],
                    page_size=25,
                    sort_action="native",
                    filter_action="native",
                    style_cell={
                        "backgroundColor": "#16213e", "color": "#eceff1",
                        "fontSize": "0.78rem", "padding": "5px 7px",
                        "fontFamily": "monospace", "textAlign": "right",
                    },
                    style_cell_conditional=[
                        {"if": {"column_id": "ticker"}, "textAlign": "left"},
                        {"if": {"column_id": "name"}, "textAlign": "left",
                         "fontFamily": "inherit"},
                        {"if": {"column_id": "sector"}, "textAlign": "left",
                         "fontFamily": "inherit"},
                        {"if": {"column_id": "status_badge"}, "textAlign": "center"},
                    ],
                    style_header={
                        "backgroundColor": "#1a1a2e", "color": "#90caf9",
                        "fontWeight": "bold", "fontSize": "0.72rem",
                    },
                    style_data_conditional=[
                        # Highlight composite percentile by tier
                        {"if": {"filter_query": "{composite_pctile} >= 90",
                                "column_id": "composite_pctile"},
                         "color": "#00c853", "fontWeight": "bold"},
                        {"if": {"filter_query":
                                "{composite_pctile} >= 75 && {composite_pctile} < 90",
                                "column_id": "composite_pctile"},
                         "color": "#69f0ae", "fontWeight": "bold"},
                        {"if": {"filter_query": "{composite_pctile} <= 25",
                                "column_id": "composite_pctile"},
                         "color": "#ff8a65"},
                        # Highlight flagged tickers
                        {"if": {"filter_query": '{status_badge} contains "FLAG"'},
                         "backgroundColor": "#2a1810"},
                        {"if": {"filter_query": '{status_badge} contains "DQ"'},
                         "backgroundColor": "#1a1a1a", "color": "#607d8b"},
                    ],
                    style_filter={"backgroundColor": "#0f1a2e", "color": "#eceff1"},
                ),
            ]),
        ], style=CARD_STYLE),
    ])
