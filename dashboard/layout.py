from dash import dcc, html
import dash_bootstrap_components as dbc


def build_layout(sectors: list[str]) -> html.Div:
    return html.Div([
        _header_bar(),
        dbc.Container([
            dcc.Interval(id="auto-refresh", interval=60_000, n_intervals=0),
            dcc.Store(id="selected-sector", data=None),

            # Sector direction cards
            dbc.Row(id="sector-cards", className="mb-3 g-3"),

            # Debug indicator — shows selected sector on click
            html.Div([
                html.Span("Selected: ", className="text-muted small me-1"),
                html.Span(id="debug-selected", children="(none)", className="text-warning small fw-bold"),
            ], className="mb-2 ms-1"),

            # Main content row
            dbc.Row([
                # Left sidebar: controls + heatmap
                dbc.Col([
                    _controls_panel(),
                    html.Div(id="sector-heatmap-container", className="mt-3"),
                ], width=3),

                # Right: sector detail (always in DOM)
                dbc.Col([
                    _sector_detail_panel(),
                ], width=9),
            ]),
        ], fluid=True, className="py-3"),
    ], style={"background": "#0f0f23", "minHeight": "100vh"})


def _header_bar():
    return dbc.Navbar(
        dbc.Container([
            dbc.Row([
                dbc.Col(html.Span(
                    "HK & China Market Sentiment", className="text-white fw-bold fs-4"
                )),
                dbc.Col([
                    html.Span("Last updated: ", className="text-muted small me-1"),
                    html.Span(id="last-updated", className="text-info small"),
                ], className="text-end"),
            ], align="center", className="w-100"),
        ], fluid=True),
        color="#1a1a2e",
        dark=True,
        className="mb-3 border-bottom border-secondary",
    )


def _controls_panel():
    return dbc.Card([
        dbc.CardHeader("Controls", className="fw-bold small"),
        dbc.CardBody([
            dbc.Button("Refresh Now", id="refresh-btn", color="primary",
                       size="sm", className="w-100 mb-3"),
            html.Div(id="scraper-status", className="small"),
        ]),
    ], style={"background": "#1a1a2e", "border": "1px solid #37474f"})


def _sector_detail_panel():
    """Sector detail panel -- all components always in the DOM."""
    placeholder = html.P("Click a sector card above to see detailed analysis.",
                         className="text-muted text-center py-4")
    return dbc.Card([
        dbc.CardHeader([
            dbc.Row([
                dbc.Col(html.Span(id="sector-detail-title",
                                  children="Sector Detail",
                                  className="fw-bold fs-5 text-light")),
                dbc.Col([
                    dbc.Badge(id="sector-direction-badge", children="--",
                              color="secondary", className="fs-6 me-2"),
                    html.Span(id="sector-confidence-text", className="text-muted small"),
                    html.Br(),
                    html.Span(id="sector-signal-updated", className="text-muted",
                              style={"fontSize": "0.7rem"}),
                ], className="text-end"),
            ], align="center"),
        ]),
        dbc.CardBody([
            # Placeholder shown before any sector is selected
            html.Div(id="sector-detail-placeholder", children=placeholder),

            # All detail content (hidden until a sector is selected)
            html.Div(id="sector-detail-content", style={"display": "none"}, children=[
                # Top row: gauge + source pie
                dbc.Row([
                    dbc.Col(dcc.Graph(id="sector-gauge",
                                     config={"displayModeBar": False},
                                     figure={}), width=5),
                    dbc.Col(dcc.Graph(id="sector-source-pie",
                                     config={"displayModeBar": False},
                                     figure={}), width=7),
                ], className="mb-3"),

                # Sector sentiment timeseries
                dcc.Graph(id="sector-sentiment-ts",
                          config={"displayModeBar": False}, figure={}),

                # Ticker breakdown within sector
                dbc.Card([
                    dbc.CardHeader("Ticker Breakdown (within sector)",
                                  className="fw-bold small"),
                    dbc.CardBody([
                        dbc.Row([
                            dbc.Col(dcc.Graph(id="ticker-breakdown-bar",
                                             config={"displayModeBar": False},
                                             figure={}), width=5),
                            dbc.Col(html.Div(id="ticker-rows"), width=7),
                        ]),
                    ]),
                ], className="mb-3",
                   style={"background": "#16213e", "border": "1px solid #37474f"}),

                # AI-generated sector analysis
                dbc.Card([
                    dbc.CardHeader("AI Sector Analysis", className="fw-bold small"),
                    dbc.CardBody(
                        dcc.Loading(
                            html.Div(id="sector-ai-analysis"),
                            type="dot", color="#00c853",
                        )
                    ),
                ], className="mb-3",
                   style={"background": "#16213e", "border": "1px solid #37474f"}),

                # Article feed for this sector
                dbc.Card([
                    dbc.CardHeader("Recent Articles", className="fw-bold small"),
                    dbc.CardBody(html.Div(id="sector-article-feed")),
                ], style={"background": "#16213e", "border": "1px solid #37474f"}),
            ]),
        ]),
    ], style={"background": "#1a1a2e", "border": "1px solid #37474f"})
