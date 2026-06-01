from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from dashboard import theme as T

FLAG_COLORS = {"high": T.DANGER, "medium": T.WARNING, "low": T.INFO}


def _stat_block(label: str, value_id: str, color: str = None):
    return html.Div([
        html.Div(label, className="stat-label"),
        html.Div(id=value_id, className="hero-number",
                  style={"color": color} if color else {}),
    ])


def build_recommendations_tab() -> html.Div:
    """The Discovery tab — multi-factor percentile-rank candidates for research."""
    return html.Div([
        dcc.Interval(id="rec-auto-refresh", interval=300_000, n_intervals=0),

        # Caveat banner
        dbc.Alert([
            html.Strong("Discovery, not recommendations. "),
            "Candidates for further research, not buy/sell advice. ",
            "Scores are sector-relative percentile ranks (0=worst, 100=best). ",
            "A high composite rank means 'looks attractive across these factors vs sector peers' — ",
            "it does NOT account for business catalysts, governance, capital structure, or qualitative risks. ",
            "Flagged tickers carry known macro/regulatory issues that may distort valuation; ",
            "they remain visible but should be researched extra-carefully.",
        ], color="info", className="small mb-3", dismissable=True),

        dbc.Alert(id="rec-diagnostic-banner", color="warning", className="small mb-3",
                  is_open=False, dismissable=True),

        # Stats strip — 3 hero numbers
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(_stat_block("Scorable", "rec-stat-scorable",
                                          color=T.PRIMARY), width=3),
                    dbc.Col(_stat_block("Disqualified", "rec-stat-disqualified",
                                          color=T.WARNING), width=3),
                    dbc.Col(_stat_block("Flagged", "rec-stat-flagged",
                                          color=T.DANGER), width=3),
                    dbc.Col(
                        dbc.Button("Recompute", id="rec-refresh-btn", color="primary",
                                   size="sm", className="float-end mt-3"),
                        width=3),
                ], align="center"),
            ], style={"padding": "20px 24px"}),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Factor weight controls
        dbc.Card([
            dbc.CardHeader([
                html.Span("Factor Weights", style={"fontWeight": "600",
                                                    "marginRight": "10px"}),
                html.Span(id="rec-weights-normalized",
                          style={"color": T.PRIMARY, "fontSize": "0.85rem",
                                 "fontWeight": "500"}),
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Value (cheap)", className="stat-label mb-2"),
                        dbc.InputGroup([
                            dbc.Input(id="rec-weight-value", type="number",
                                      min=0, max=100, step=5, value=30,
                                      size="sm", style=T.INPUT_STYLE),
                            dbc.InputGroupText("%", style=T.INPUT_SUFFIX_STYLE),
                        ], size="sm"),
                    ], width=3),
                    dbc.Col([
                        html.Label("Quality (ROE, low debt)", className="stat-label mb-2"),
                        dbc.InputGroup([
                            dbc.Input(id="rec-weight-quality", type="number",
                                      min=0, max=100, step=5, value=30,
                                      size="sm", style=T.INPUT_STYLE),
                            dbc.InputGroupText("%", style=T.INPUT_SUFFIX_STYLE),
                        ], size="sm"),
                    ], width=3),
                    dbc.Col([
                        html.Label("Growth (earnings, revenue)", className="stat-label mb-2"),
                        dbc.InputGroup([
                            dbc.Input(id="rec-weight-growth", type="number",
                                      min=0, max=100, step=5, value=20,
                                      size="sm", style=T.INPUT_STYLE),
                            dbc.InputGroupText("%", style=T.INPUT_SUFFIX_STYLE),
                        ], size="sm"),
                    ], width=3),
                    dbc.Col([
                        html.Label("Sentiment (news mood)", className="stat-label mb-2"),
                        dbc.InputGroup([
                            dbc.Input(id="rec-weight-sentiment", type="number",
                                      min=0, max=100, step=5, value=20,
                                      size="sm", style=T.INPUT_STYLE),
                            dbc.InputGroupText("%", style=T.INPUT_SUFFIX_STYLE),
                        ], size="sm"),
                    ], width=3),
                ], className="g-2"),
                html.Div([
                    html.Label("Sentiment window (days)",
                                className="stat-label mb-2 mt-3"),
                    dcc.Slider(
                        id="rec-window-slider",
                        min=1, max=30, step=1, value=7,
                        marks={1: "1d", 7: "7d", 14: "14d", 30: "30d"},
                        tooltip={"placement": "bottom", "always_visible": False},
                    ),
                ]),
                html.Hr(style={"borderColor": T.BORDER, "margin": "20px 0"}),
                html.Label("Filters", className="stat-label mb-2"),
                dbc.Row([
                    dbc.Col([
                        html.Label("Min composite percentile",
                                    className="stat-label mb-2"),
                        dcc.Slider(
                            id="rec-min-composite-filter",
                            min=0, max=100, step=5, value=0,
                            marks={0: "0", 50: "50", 75: "75", 100: "100"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                    ], width=4),
                    dbc.Col([
                        html.Label("Show", className="stat-label mb-2"),
                        dcc.Checklist(
                            id="rec-show-filter",
                            options=[
                                {"label": " Watchlist only", "value": "watchlist"},
                                {"label": " Include flagged", "value": "include_flagged"},
                                {"label": " Include disqualified (educational)",
                                 "value": "include_dq"},
                            ],
                            value=["include_flagged"],
                            labelClassName="me-3 d-block",
                            style={"fontSize": "0.85rem", "color": T.TEXT},
                        ),
                    ], width=4),
                    dbc.Col([
                        html.Label("Sector", className="stat-label mb-2"),
                        dcc.Dropdown(id="rec-sector-filter", multi=True,
                                     placeholder="All sectors"),
                    ], width=4),
                ]),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Composite distribution chart
        dbc.Card([
            dbc.CardHeader("Composite Percentile Distribution"),
            dbc.CardBody([
                dcc.Graph(id="rec-distribution-chart",
                          config={"displayModeBar": False}, figure={}),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Ranked table
        dbc.Card([
            dbc.CardHeader([
                html.Span("Discovery Candidates",
                          style={"fontWeight": "600", "marginRight": "10px"}),
                html.Span(id="rec-row-count",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.85rem"}),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id="rec-table",
                    columns=[
                        {"name": "Ticker",        "id": "ticker"},
                        {"name": "Name",          "id": "name"},
                        {"name": "Sector",        "id": "sector"},
                        {"name": "Composite %",   "id": "composite_pctile", "type": "numeric"},
                        {"name": "Value %",       "id": "value_pctile",     "type": "numeric"},
                        {"name": "Quality %",     "id": "quality_pctile",   "type": "numeric"},
                        {"name": "Growth %",      "id": "growth_pctile",    "type": "numeric"},
                        {"name": "Sentiment %",   "id": "sentiment_pctile", "type": "numeric"},
                        {"name": "Articles",      "id": "article_count",    "type": "numeric"},
                        {"name": "P/E",           "id": "trailing_pe",      "type": "numeric"},
                        {"name": "ROE %",         "id": "roe_display",      "type": "numeric"},
                        {"name": "Earn Growth %", "id": "earn_growth_display", "type": "numeric"},
                        {"name": "Mkt Cap (B)",   "id": "market_cap_b",     "type": "numeric"},
                        {"name": "Status",        "id": "status_badge"},
                    ],
                    data=[],
                    page_size=25,
                    sort_action="native",
                    filter_action="native",
                    style_cell=T.DATATABLE_CELL,
                    style_cell_conditional=[
                        {"if": {"column_id": "ticker"}, "textAlign": "left",
                         "fontWeight": "600", "color": T.PRIMARY},
                        {"if": {"column_id": "name"}, "textAlign": "left",
                         "fontFamily": "Inter, sans-serif"},
                        {"if": {"column_id": "sector"}, "textAlign": "left",
                         "fontFamily": "Inter, sans-serif", "color": T.TEXT_MUTED},
                        {"if": {"column_id": "status_badge"}, "textAlign": "center"},
                    ],
                    style_header=T.DATATABLE_HEADER,
                    style_data_conditional=[
                        {"if": {"filter_query": "{composite_pctile} >= 90",
                                "column_id": "composite_pctile"},
                         "color": T.SUCCESS, "fontWeight": "700"},
                        {"if": {"filter_query":
                                "{composite_pctile} >= 75 && {composite_pctile} < 90",
                                "column_id": "composite_pctile"},
                         "color": T.SUCCESS, "fontWeight": "600"},
                        {"if": {"filter_query": "{composite_pctile} <= 25",
                                "column_id": "composite_pctile"},
                         "color": T.DANGER},
                        {"if": {"filter_query": '{status_badge} contains "FLAG"'},
                         "backgroundColor": T.WARNING_SOFT},
                        {"if": {"filter_query": '{status_badge} contains "DQ"'},
                         "backgroundColor": T.CARD_BG_SOFT, "color": T.TEXT_FAINT},
                    ],
                    style_filter=T.DATATABLE_FILTER,
                ),
            ]),
        ], style=T.CARD_STYLE),
    ])
