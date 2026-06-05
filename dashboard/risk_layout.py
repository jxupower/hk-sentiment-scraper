"""Risk Forecast tab — GJR-GARCH(1,1)-t volatility forecasting + Monte
Carlo simulation. All callback IDs are kept under the `risk-` prefix.
"""
from dash import dcc, html
import dash_bootstrap_components as dbc

from dashboard import theme as T


# Indices pinned to the top of the ticker selector. The "^" prefix
# distinguishes them from equities in the historical_prices table.
INDEX_OPTIONS = [
    {"label": "★ ^HSI — Hang Seng Index", "value": "^HSI"},
    {"label": "★ ^HSCEI — Hang Seng China Enterprises", "value": "^HSCEI"},
    {"label": "★ ^HSTECH — Hang Seng Tech", "value": "^HSTECH"},
]

# History-window radio: how much past data to feed the GARCH fit.
# Values are TRADING days, not calendar days — the prices list contains
# one entry per trading day and the cache slices it with `iloc[-N:]`.
# HK has ~252 trading days/year, so 252/756/1260 deliver ~1y/3y/5y.
HISTORY_WINDOW_OPTIONS = [
    {"label": "1Y", "value": 252},
    {"label": "3Y", "value": 756},
    {"label": "5Y", "value": 1260},
    {"label": "MAX", "value": 0},
]
DEFAULT_HISTORY_WINDOW = 1260   # 5Y (= 5 × 252 trading days)

# Forecast horizon radio: trading days to simulate forward.
HORIZON_OPTIONS = [
    {"label": "5d", "value": 5},
    {"label": "21d (1mo)", "value": 21},
    {"label": "63d (1qtr)", "value": 63},
]
DEFAULT_HORIZON = 21


def build_risk_tab() -> html.Div:
    return html.Div([
        dbc.Alert([
            html.Strong("Risk Forecast — GJR-GARCH(1,1) with Student-t innovations. "),
            "Pick a stock or Hang Seng index, click Load. The fan chart shows "
            "5,000 Monte Carlo paths over the chosen horizon. ",
            html.Br(),
            html.Em("Honest gaps: GARCH is slow to adapt to regime breaks; "
                    "Monte Carlo with a fitted parametric model can't generate "
                    "tail events outside the fitted distribution; forecasts "
                    "beyond ~5 days are illustrative, not precise."),
        ], color="info", className="small mb-3", dismissable=True),

        # Header — ticker + window + horizon + Load
        # Responsive: stack on phones (xs=12 each), spread across the row on
        # md+ desktops. Horizon gets width=3 (was 2) because its 3 buttons have
        # long labels ("21d (1mo)", "63d (1qtr)") that overflowed width=2.
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Ticker (index or HK stock)",
                                    className="text-muted small mb-1"),
                        dcc.Dropdown(
                            id="risk-ticker-select",
                            options=INDEX_OPTIONS,
                            value="^HSI",
                            placeholder="Type to search or pick an index above…",
                            clearable=False,
                        ),
                    ], xs=12, md=4),
                    dbc.Col([
                        html.Label("History window (fit data)",
                                    className="text-muted small mb-1"),
                        dbc.RadioItems(
                            id="risk-history-window",
                            options=HISTORY_WINDOW_OPTIONS,
                            value=DEFAULT_HISTORY_WINDOW,
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12, sm=6, md=3),
                    dbc.Col([
                        html.Label("Forecast horizon",
                                    className="text-muted small mb-1"),
                        dbc.RadioItems(
                            id="risk-horizon",
                            options=HORIZON_OPTIONS,
                            value=DEFAULT_HORIZON,
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12, sm=6, md=3),
                    dbc.Col([
                        html.Label(" ", className="small mb-1"),
                        dbc.Button("Load risk forecast", id="risk-load-btn",
                                   color="primary", size="sm", className="w-100"),
                    ], xs=12, md=2),
                ], align="end", className="g-2"),
            ], style={"padding": "12px 16px"}),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Placeholder shown until first Load
        html.Div(id="risk-placeholder",
                  children=html.P("Pick a ticker, history window, and horizon — then click Load.",
                                  className="text-muted text-center py-5"),
                  style={"display": "block"}),

        # Main content — hidden until first load
        html.Div(id="risk-content", style={"display": "none"}, children=[
            # Fan chart (the centerpiece) + status strip
            dbc.Card([
                dbc.CardHeader([
                    html.Span("Monte Carlo fan chart", className="fw-bold"),
                    html.Span(id="risk-fan-subtitle",
                              style={"color": T.TEXT_MUTED, "fontSize": "0.8rem",
                                     "marginLeft": "12px"}),
                ]),
                dbc.CardBody([
                    dcc.Loading(type="dot", color=T.PRIMARY, children=[
                        dcc.Graph(id="risk-fan-chart",
                                  config={"displayModeBar": False}, figure={}),
                    ]),
                ]),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Two-column row: vol cone (left) + risk metrics table (right)
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Forecast vs. historical volatility",
                                        className="fw-bold small"),
                        dbc.CardBody([
                            dcc.Graph(id="risk-vol-cone",
                                      config={"displayModeBar": False}, figure={}),
                        ]),
                    ], style=T.CARD_STYLE),
                ], width=7),
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Value-at-Risk & expected shortfall",
                                        className="fw-bold small"),
                        dbc.CardBody(html.Div(id="risk-var-table")),
                    ], style=T.CARD_STYLE, className="mb-2"),
                    dbc.Card([
                        dbc.CardHeader("Loss probabilities (over horizon)",
                                        className="fw-bold small"),
                        dbc.CardBody(html.Div(id="risk-prob-table")),
                    ], style=T.CARD_STYLE),
                ], width=5),
            ], className="mb-3"),

            # Drawdown histogram (full-width)
            dbc.Card([
                dbc.CardHeader("Max-drawdown distribution",
                                className="fw-bold small"),
                dbc.CardBody([
                    dcc.Graph(id="risk-drawdown-hist",
                              config={"displayModeBar": False}, figure={}),
                ]),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Model diagnostics (smallest card, at the bottom)
            dbc.Card([
                dbc.CardHeader("GJR-GARCH(1,1)-t fit diagnostics",
                                className="fw-bold small"),
                dbc.CardBody(html.Div(id="risk-diagnostics")),
            ], style=T.CARD_STYLE),
        ]),
    ])
