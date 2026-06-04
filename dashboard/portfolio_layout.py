"""Portfolio Rebalancer tab — max-Sharpe optimisation via MPT.

UI shape: holdings table (editable) + parameter controls + output cards
(three Sharpe hero numbers, weights bar chart, efficient frontier,
walk-forward backtest, candidate marginal-value table).
"""
from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from dashboard import theme as T


# Lookback in trading days (252/y); 0 = MAX.
LOOKBACK_OPTIONS = [
    {"label": "1Y",  "value": 252},
    {"label": "3Y",  "value": 756},
    {"label": "5Y",  "value": 1260},
    {"label": "MAX", "value": 0},
]
DEFAULT_LOOKBACK = 756

# Rebalance frequency in trading days.
REBALANCE_OPTIONS = [
    {"label": "5d",   "value": 5},
    {"label": "21d (1mo)",  "value": 21},
    {"label": "63d (1qtr)", "value": 63},
    {"label": "252d (1y)",  "value": 252},
]
DEFAULT_REBALANCE = 21

DEFAULT_WEIGHT_CAP_PCT = 30
DEFAULT_RF_PCT = 0.0


def build_portfolio_tab() -> html.Div:
    return html.Div([
        dbc.Alert([
            html.Strong("Portfolio Rebalancer — Max-Sharpe via Modern Portfolio Theory. "),
            "Enter your holdings (ticker + shares), add candidate tickers with 0 shares, "
            "pick a lookback + rebalance frequency, click Compute. The full universe (current + candidates) "
            "is fed through Ledoit-Wolf shrunk Σ and SLSQP to find the long-only, capped, max-Sharpe portfolio.",
            html.Br(),
            html.Em("Honest gaps: sample means are very noisy, so 'optimal weights' are a directional guide; "
                    "in-sample Sharpe is biased up by construction (the walk-forward backtest shows out-of-sample reality); "
                    "no transaction costs / taxes modelled."),
        ], color="info", className="small mb-3", dismissable=True),

        # ============== Holdings table ==============
        dbc.Card([
            dbc.CardHeader([
                html.Span("Holdings", className="fw-bold"),
                html.Span(" — type ticker (e.g. 0700.HK or ^HSI) + shares. "
                          "Use shares=0 for candidates you're considering.",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.8rem",
                                 "marginLeft": "8px"}),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id="portfolio-holdings-table",
                    columns=[
                        {"name": "Ticker", "id": "ticker", "type": "text",
                         "editable": True},
                        {"name": "Shares", "id": "shares", "type": "numeric",
                         "editable": True},
                    ],
                    data=[
                        {"ticker": "0700.HK", "shares": 100},
                        {"ticker": "0005.HK", "shares": 200},
                        {"ticker": "9988.HK", "shares": 50},
                        {"ticker": "1810.HK", "shares": 0},
                        {"ticker": "0823.HK", "shares": 0},
                    ],
                    editable=True,
                    row_deletable=True,
                    style_cell=T.DATATABLE_CELL,
                    style_header=T.DATATABLE_HEADER,
                    style_cell_conditional=[
                        {"if": {"column_id": "ticker"}, "textAlign": "left",
                         "fontWeight": "600", "color": T.PRIMARY,
                         "width": "60%"},
                        {"if": {"column_id": "shares"}, "textAlign": "right",
                         "width": "40%"},
                    ],
                ),
                html.Div([
                    dbc.Button("+ Add row", id="portfolio-add-row-btn",
                                color="secondary", size="sm",
                                outline=True, className="mt-2 me-2"),
                    html.Span(id="portfolio-holdings-status",
                              style={"color": T.TEXT_MUTED, "fontSize": "0.8rem"}),
                ]),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # ============== Parameters ==============
        dbc.Card([
            dbc.CardHeader("Parameters", className="fw-bold"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Lookback (estimation window)",
                                    className="text-muted small mb-1"),
                        dbc.RadioItems(
                            id="portfolio-lookback",
                            options=LOOKBACK_OPTIONS, value=DEFAULT_LOOKBACK,
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], width=3),
                    dbc.Col([
                        html.Label("Rebalance frequency (backtest)",
                                    className="text-muted small mb-1"),
                        dbc.RadioItems(
                            id="portfolio-rebalance",
                            options=REBALANCE_OPTIONS, value=DEFAULT_REBALANCE,
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], width=4),
                    dbc.Col([
                        html.Label([
                            "Per-asset cap ",
                            html.Span(id="portfolio-cap-label",
                                       style={"color": T.PRIMARY,
                                              "fontWeight": "600"}),
                        ], className="text-muted small mb-1"),
                        dcc.Slider(
                            id="portfolio-cap-slider",
                            min=5, max=100, step=5,
                            value=DEFAULT_WEIGHT_CAP_PCT,
                            marks={10: "10%", 30: "30%", 50: "50%", 100: "100%"},
                            tooltip={"placement": "bottom"},
                        ),
                    ], width=3),
                    dbc.Col([
                        html.Label("Risk-free rate (%, ann.)",
                                    className="text-muted small mb-1"),
                        dcc.Input(
                            id="portfolio-rf",
                            type="number", min=0, max=20, step=0.5,
                            value=DEFAULT_RF_PCT,
                            style={**T.INPUT_STYLE, "width": "100%",
                                   "padding": "5px 10px"},
                        ),
                    ], width=2),
                ]),
                html.Div([
                    dbc.Button("Compute optimal portfolio",
                                id="portfolio-compute-btn",
                                color="primary", size="sm", className="mt-3"),
                    html.Span(id="portfolio-compute-status",
                              style={"color": T.TEXT_MUTED, "fontSize": "0.8rem",
                                     "marginLeft": "12px"}),
                ]),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # ============== Placeholder + content ==============
        html.Div(id="portfolio-placeholder",
                  children=html.P("Enter holdings, pick parameters, then click Compute.",
                                  className="text-muted text-center py-5"),
                  style={"display": "block"}),

        html.Div(id="portfolio-content", style={"display": "none"}, children=[

            # Three Sharpe hero numbers
            dbc.Card([
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col(_sharpe_hero("Status quo",
                                              "portfolio-sharpe-status",
                                              T.TEXT_MUTED), width=4),
                        dbc.Col(_sharpe_hero("Current-only optimum",
                                              "portfolio-sharpe-current",
                                              T.INFO), width=4),
                        dbc.Col(_sharpe_hero("Full-universe optimum",
                                              "portfolio-sharpe-full",
                                              T.SUCCESS), width=4),
                    ]),
                    dbc.Row([
                        dbc.Col(html.Div(id="portfolio-sharpe-delta-rebal",
                                          className="text-center small"),
                                 width=6),
                        dbc.Col(html.Div(id="portfolio-sharpe-delta-add",
                                          className="text-center small"),
                                 width=6),
                    ], className="mt-2"),
                ]),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Weights bar + frontier
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Weights — current vs. optimal",
                                        className="fw-bold small"),
                        dbc.CardBody(dcc.Graph(id="portfolio-weights-chart",
                                                config={"displayModeBar": False},
                                                figure={})),
                    ], style=T.CARD_STYLE),
                ], width=6),
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Efficient frontier",
                                        className="fw-bold small"),
                        dbc.CardBody(dcc.Graph(id="portfolio-frontier-chart",
                                                config={"displayModeBar": False},
                                                figure={})),
                    ], style=T.CARD_STYLE),
                ], width=6),
            ], className="mb-3"),

            # Walk-forward backtest
            dbc.Card([
                dbc.CardHeader("Walk-forward backtest",
                                className="fw-bold small"),
                dbc.CardBody([
                    dcc.Graph(id="portfolio-backtest-chart",
                              config={"displayModeBar": False}, figure={}),
                    html.Div(id="portfolio-backtest-table", className="mt-2"),
                ]),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Candidate marginal value + rebalance trade list
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Candidate marginal value",
                                        className="fw-bold small"),
                        dbc.CardBody(html.Div(id="portfolio-candidate-table")),
                    ], style=T.CARD_STYLE),
                ], width=6),
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Rebalance trade list (to reach full-optimal)",
                                        className="fw-bold small"),
                        dbc.CardBody(html.Div(id="portfolio-trade-list")),
                    ], style=T.CARD_STYLE),
                ], width=6),
            ], className="mb-3"),

            # Diagnostics
            dbc.Card([
                dbc.CardHeader("Estimation diagnostics",
                                className="fw-bold small"),
                dbc.CardBody(html.Div(id="portfolio-diagnostics")),
            ], style=T.CARD_STYLE),
        ]),
    ])


def _sharpe_hero(label: str, value_id: str, color: str):
    return html.Div([
        html.Div(label, style={"color": T.TEXT_MUTED, "fontSize": "0.72rem",
                                "fontWeight": "600", "letterSpacing": "0.05em",
                                "textTransform": "uppercase",
                                "textAlign": "center"}),
        html.Div(id=value_id, style={**T.HERO_NUMBER_STYLE,
                                       "color": color,
                                       "textAlign": "center"}),
    ])
