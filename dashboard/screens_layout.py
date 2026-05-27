from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from analysis.screens import BUILTIN_SCREENS

CARD_STYLE = {"background": "#1a1a2e", "border": "1px solid #37474f"}


def build_screens_tab() -> html.Div:
    """Rule-based screens tab — absolute-threshold filters, no scoring."""
    return html.Div([
        dcc.Interval(id="screens-auto-refresh", interval=600_000, n_intervals=0),

        dbc.Alert([
            html.Strong("Rule-based screens. "),
            "Stocks either pass or don't pass each screen — no ranking, no scoring. "
            "Use absolute valuation/quality thresholds that disciplined value investors care about. "
            "Compare with the Discovery tab (percentile ranks) to see how the two approaches differ.",
        ], color="info", className="small mb-3", dismissable=True),

        # One tab per screen
        dbc.Tabs(
            id="screen-subtabs",
            active_tab="screen-tab-value",
            className="mb-3",
            children=[
                dbc.Tab(
                    label=screen.name,
                    tab_id=f"screen-tab-{screen.id}",
                    labelClassName="text-light",
                    active_label_style={"color": "#90caf9", "fontWeight": "bold"},
                    children=_build_screen_subtab(screen),
                )
                for screen in BUILTIN_SCREENS
            ],
        ),
    ])


def _build_screen_subtab(screen) -> html.Div:
    return html.Div([
        dbc.Card([
            dbc.CardHeader([
                html.Span(screen.name, className="fw-bold me-2"),
                html.Span(id=f"screen-{screen.id}-count",
                          className="text-info small"),
            ]),
            dbc.CardBody([
                html.P(screen.long_description, className="text-light small mb-3"),
                html.P([
                    html.Strong("Note: ", className="text-warning small"),
                    html.Span(
                        "Pass/fail screens are coarser than percentile ranking — "
                        "a stock just outside a threshold won't appear. Use Discovery "
                        "for nuanced ranking, Screens for confidence.",
                        className="text-muted small"
                    ),
                ], className="mb-2"),
            ], style={"padding": "12px 16px"}),
        ], style=CARD_STYLE, className="mb-3"),

        dbc.Card([
            dbc.CardHeader([
                html.Span("Matching Tickers", className="fw-bold small me-2"),
                html.Span(id=f"screen-{screen.id}-meta",
                          className="text-muted small"),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id=f"screen-{screen.id}-table",
                    columns=[
                        {"name": "Ticker",     "id": "ticker"},
                        {"name": "Name",       "id": "name"},
                        {"name": "Sector",     "id": "sector"},
                        {"name": "Mkt Cap (B)","id": "market_cap_b", "type": "numeric"},
                        {"name": "P/E",        "id": "trailing_pe",  "type": "numeric"},
                        {"name": "P/B",        "id": "price_to_book","type": "numeric"},
                        {"name": "Div Y %",    "id": "dividend_yield","type": "numeric"},
                        {"name": "ROE %",      "id": "roe_display",  "type": "numeric"},
                        {"name": "D/E %",      "id": "debt_to_equity","type": "numeric"},
                        {"name": "Earn Gr %",  "id": "earn_growth_display","type": "numeric"},
                        {"name": "Status",     "id": "status_badge"},
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
                        {"if": {"filter_query": '{status_badge} contains "★"'},
                         "backgroundColor": "#1f2942"},
                        {"if": {"filter_query": '{status_badge} contains "FLAG"'},
                         "backgroundColor": "#2a1810"},
                    ],
                    style_filter={"backgroundColor": "#0f1a2e", "color": "#eceff1"},
                ),
            ]),
        ], style=CARD_STYLE),
    ])
