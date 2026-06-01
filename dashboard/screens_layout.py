from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from analysis.screens import BUILTIN_SCREENS
from dashboard import theme as T


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

        dbc.Tabs(
            id="screen-subtabs",
            active_tab="screen-tab-value",
            className="mb-3",
            children=[
                dbc.Tab(
                    label=screen.name,
                    tab_id=f"screen-tab-{screen.id}",
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
                html.Span(screen.name, style={"fontWeight": "600",
                                                "marginRight": "10px"}),
                html.Span(id=f"screen-{screen.id}-count",
                          style={"color": T.PRIMARY, "fontWeight": "600",
                                 "fontSize": "0.85rem"}),
            ]),
            dbc.CardBody([
                html.P(screen.long_description,
                       style={"color": T.TEXT, "fontSize": "0.9rem",
                              "lineHeight": "1.6", "marginBottom": "12px"}),
                html.P([
                    html.Strong("Note: ", style={"color": T.WARNING}),
                    html.Span(
                        "Pass/fail screens are coarser than percentile ranking — "
                        "a stock just outside a threshold won't appear. Use Discovery "
                        "for nuanced ranking, Screens for confidence.",
                        style={"color": T.TEXT_MUTED, "fontSize": "0.85rem"}),
                ]),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        dbc.Card([
            dbc.CardHeader([
                html.Span("Matching Tickers",
                          style={"fontWeight": "600", "marginRight": "10px"}),
                html.Span(id=f"screen-{screen.id}-meta",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.85rem"}),
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
                        {"if": {"filter_query": '{status_badge} contains "★"'},
                         "backgroundColor": T.PRIMARY_SOFT},
                        {"if": {"filter_query": '{status_badge} contains "FLAG"'},
                         "backgroundColor": T.WARNING_SOFT},
                    ],
                    style_filter=T.DATATABLE_FILTER,
                ),
            ]),
        ], style=T.CARD_STYLE),
    ])
