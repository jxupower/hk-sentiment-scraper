from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from analysis.screens import BUILTIN_SCREENS
from dashboard import theme as T

OPTIMIZABLE_SCREENS = [s for s in BUILTIN_SCREENS if s.id != "avoid_distress"]


def _stat_block(label: str, value_id: str, color: str = None):
    return html.Div([
        html.Div(label, className="stat-label"),
        html.Div(id=value_id, className="hero-number",
                  style={"color": color, "fontSize": "1.4rem"} if color
                         else {"fontSize": "1.4rem"}),
    ])


def build_backtest_tab() -> html.Div:
    return html.Div([
        dcc.Interval(id="bt-auto-refresh", interval=600_000, n_intervals=0),

        dbc.Alert([
            html.Strong("Walk-forward backtest of fundamental screens. "),
            "Default parameters tested against sector-equal-weighted benchmark. ",
            "Per-industry 'optimal' parameters are the grid combos that historically ",
            "produced the highest Information Ratio across walk-forward windows. ",
            html.Strong("Known limitations: ", className="ms-2"),
            "akshare data is as-restated (60-day reporting lag applied as partial mitigation); ",
            "survivor bias from delisted-ticker gaps; no transaction costs; equal-weighted only.",
        ], color="info", className="small mb-3", dismissable=True),

        dbc.Tabs(
            id="bt-subtabs",
            active_tab=f"bt-tab-{OPTIMIZABLE_SCREENS[0].id}",
            className="mb-3",
            children=[
                dbc.Tab(label=s.name, tab_id=f"bt-tab-{s.id}",
                        children=_build_screen_subtab(s))
                for s in OPTIMIZABLE_SCREENS
            ],
        ),
    ])


def _build_screen_subtab(screen) -> html.Div:
    return html.Div([
        # Top stats strip
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(_stat_block("Last optimized",
                                          f"bt-{screen.id}-last-opt",
                                          color=T.PRIMARY), width=4),
                    dbc.Col(_stat_block("Industries scored",
                                          f"bt-{screen.id}-n-industries"), width=4),
                    dbc.Col(_stat_block("Avg IR (across industries)",
                                          f"bt-{screen.id}-avg-ir",
                                          color=T.WARNING), width=4),
                ], align="center"),
                html.Div(id=f"bt-{screen.id}-status",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.85rem",
                                 "marginTop": "12px"}),
            ], style={"padding": "20px 24px"}),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Per-industry optimal parameter table
        dbc.Card([
            dbc.CardHeader(f"Per-Industry Optimal Parameters — {screen.name}"),
            dbc.CardBody([
                dash_table.DataTable(
                    id=f"bt-{screen.id}-table",
                    columns=_columns_for_screen(screen.id),
                    data=[],
                    page_size=15,
                    sort_action="native",
                    style_cell=T.DATATABLE_CELL,
                    style_cell_conditional=[
                        {"if": {"column_id": "industry"}, "textAlign": "left",
                         "fontWeight": "600", "color": T.TEXT,
                         "fontFamily": "Inter, sans-serif"},
                    ],
                    style_header=T.DATATABLE_HEADER,
                    style_data_conditional=[
                        {"if": {"filter_query": "{information_ratio} >= 0.5",
                                "column_id": "information_ratio"},
                         "color": T.SUCCESS, "fontWeight": "700"},
                        {"if": {"filter_query": "{information_ratio} <= -0.2",
                                "column_id": "information_ratio"},
                         "color": T.DANGER},
                    ],
                ),
                html.P([
                    "Information Ratio > 0 means the screen historically outperformed the ",
                    "sector benchmark; higher is better. Re-run optimization with the CLI: ",
                    html.Code(f"python main.py backtest optimize --screen {screen.id}",
                              style={"color": T.PRIMARY,
                                     "background": T.PRIMARY_SOFT,
                                     "padding": "2px 6px", "borderRadius": "4px"}),
                ], style={"color": T.TEXT_MUTED, "fontSize": "0.85rem",
                          "marginTop": "12px"}),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Live ad-hoc backtest panel
        dbc.Card([
            dbc.CardHeader([
                html.Span(f"Live Backtest — {screen.name} (default params, all industries)",
                          style={"fontWeight": "600", "marginRight": "10px"}),
                dbc.Button("Run", id=f"bt-{screen.id}-run-btn",
                           color="primary", size="sm", className="ms-2"),
            ]),
            dbc.CardBody([
                html.Div(id=f"bt-{screen.id}-run-result",
                          style={"color": T.TEXT, "fontSize": "0.9rem"}),
            ]),
        ], style=T.CARD_STYLE),
    ])


def _columns_for_screen(screen_id: str) -> list[dict]:
    common = [
        {"name": "Industry",            "id": "industry"},
        {"name": "Info Ratio",          "id": "information_ratio", "type": "numeric"},
        {"name": "Windows",             "id": "n_walk_forward_windows", "type": "numeric"},
        {"name": "Last optimized",      "id": "last_optimized_at"},
    ]
    if screen_id == "value":
        return common + [
            {"name": "P/E max",         "id": "pe_max", "type": "numeric"},
            {"name": "P/B max",         "id": "pb_max", "type": "numeric"},
            {"name": "ROE min",         "id": "roe_min_pct", "type": "numeric"},
            {"name": "Earn gr min",     "id": "earnings_growth_min_pct", "type": "numeric"},
            {"name": "Mkt cap min (B)", "id": "market_cap_min_b", "type": "numeric"},
        ]
    if screen_id == "quality_compounder":
        return common + [
            {"name": "ROE min",         "id": "roe_min_pct", "type": "numeric"},
            {"name": "D/E max",         "id": "de_max", "type": "numeric"},
            {"name": "Earn gr min",     "id": "earnings_growth_min_pct", "type": "numeric"},
            {"name": "Mkt cap min (B)", "id": "market_cap_min_b", "type": "numeric"},
        ]
    if screen_id == "income":
        return common + [
            {"name": "Div Y min",       "id": "dividend_yield_min", "type": "numeric"},
            {"name": "Mkt cap min (B)", "id": "market_cap_min_b", "type": "numeric"},
            {"name": "Earn gr min",     "id": "earnings_growth_min_pct", "type": "numeric"},
        ]
    return common
