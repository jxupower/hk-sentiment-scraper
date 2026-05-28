from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from analysis.screens import BUILTIN_SCREENS

CARD_STYLE = {"background": "#1a1a2e", "border": "1px solid #37474f"}

# Screens that can be optimized (avoid_distress is educational; not for IR-maximization)
OPTIMIZABLE_SCREENS = [s for s in BUILTIN_SCREENS if s.id != "avoid_distress"]


def build_backtest_tab() -> html.Div:
    """Backtest tab — performance curves + per-industry optimal parameter tables."""
    return html.Div([
        dcc.Interval(id="bt-auto-refresh", interval=600_000, n_intervals=0),

        # Caveat banner
        dbc.Alert([
            html.Strong("Walk-forward backtest of fundamental screens. "),
            "Default parameters tested against sector-equal-weighted benchmark. ",
            "Per-industry 'optimal' parameters are the grid combos that historically ",
            "produced the highest Information Ratio across walk-forward windows. ",
            html.Strong("Known limitations: ", className="ms-2"),
            "akshare data is as-restated (60-day reporting lag applied as partial mitigation); ",
            "survivor bias from delisted-ticker gaps; no transaction costs; equal-weighted only.",
        ], color="info", className="small mb-3", dismissable=True),

        # Sub-tabs per screen
        dbc.Tabs(
            id="bt-subtabs",
            active_tab=f"bt-tab-{OPTIMIZABLE_SCREENS[0].id}",
            className="mb-3",
            children=[
                dbc.Tab(
                    label=s.name,
                    tab_id=f"bt-tab-{s.id}",
                    labelClassName="text-light",
                    active_label_style={"color": "#90caf9", "fontWeight": "bold"},
                    children=_build_screen_subtab(s),
                )
                for s in OPTIMIZABLE_SCREENS
            ],
        ),
    ])


def _build_screen_subtab(screen) -> html.Div:
    """One sub-tab per screen showing performance + per-industry params."""
    return html.Div([
        # Top stats strip
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Span("Last optimized: ", className="text-muted small me-1"),
                        html.Span(id=f"bt-{screen.id}-last-opt", className="text-info small fw-bold"),
                    ], width=4),
                    dbc.Col([
                        html.Span("Industries scored: ", className="text-muted small me-1"),
                        html.Span(id=f"bt-{screen.id}-n-industries", className="text-light fw-bold"),
                    ], width=4),
                    dbc.Col([
                        html.Span("Avg IR (across industries): ", className="text-muted small me-1"),
                        html.Span(id=f"bt-{screen.id}-avg-ir", className="text-warning fw-bold"),
                    ], width=4),
                ], align="center"),
                html.Div(id=f"bt-{screen.id}-status", className="text-muted small mt-2"),
            ], style={"padding": "10px 16px"}),
        ], style=CARD_STYLE, className="mb-3"),

        # Per-industry optimal parameter table
        dbc.Card([
            dbc.CardHeader(f"Per-Industry Optimal Parameters — {screen.name}",
                          className="fw-bold small"),
            dbc.CardBody([
                dash_table.DataTable(
                    id=f"bt-{screen.id}-table",
                    columns=_columns_for_screen(screen.id),
                    data=[],
                    page_size=15,
                    sort_action="native",
                    style_cell={
                        "backgroundColor": "#16213e", "color": "#eceff1",
                        "fontSize": "0.8rem", "padding": "5px 7px",
                        "fontFamily": "monospace", "textAlign": "right",
                    },
                    style_cell_conditional=[
                        {"if": {"column_id": "industry"}, "textAlign": "left",
                         "fontFamily": "inherit"},
                    ],
                    style_header={
                        "backgroundColor": "#1a1a2e", "color": "#90caf9",
                        "fontWeight": "bold", "fontSize": "0.72rem",
                    },
                    style_data_conditional=[
                        {"if": {"filter_query": "{information_ratio} >= 0.5",
                                "column_id": "information_ratio"},
                         "color": "#00c853", "fontWeight": "bold"},
                        {"if": {"filter_query": "{information_ratio} <= -0.2",
                                "column_id": "information_ratio"},
                         "color": "#ff8a65"},
                    ],
                ),
                html.P([
                    "Information Ratio > 0 means the screen historically outperformed the ",
                    "sector benchmark; higher is better. Re-run optimization with the CLI: ",
                    html.Code(f"python main.py backtest optimize --screen {screen.id}",
                              className="text-info"),
                ], className="text-muted small mt-2"),
            ]),
        ], style=CARD_STYLE, className="mb-3"),

        # Live ad-hoc backtest panel (defaults params, full universe)
        dbc.Card([
            dbc.CardHeader([
                html.Span(f"Live Backtest — {screen.name} (default params, all industries)",
                         className="fw-bold small me-2"),
                dbc.Button("Run", id=f"bt-{screen.id}-run-btn",
                           color="primary", size="sm", className="ms-2"),
            ]),
            dbc.CardBody([
                html.Div(id=f"bt-{screen.id}-run-result", className="text-light small"),
            ]),
        ], style=CARD_STYLE),
    ])


def _columns_for_screen(screen_id: str) -> list[dict]:
    """The displayed parameter columns differ per screen."""
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
