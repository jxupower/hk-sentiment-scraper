import json
import math
import os
from datetime import date as date_cls

from dash import Input, Output, State, html, no_update
import dash_bootstrap_components as dbc

from analysis.backtest import BacktestEngine
from analysis.screens import BUILTIN_SCREENS, ScreenParams
from dashboard.backtest_layout import OPTIMIZABLE_SCREENS


def register_backtest_callbacks(app, db_path: str):
    sector_risk_path = os.path.join(os.path.dirname(__file__), "..", "config",
                                     "sector_risk.yaml")

    # One pair of callbacks per screen tab — display optimized params table + live-backtest button
    for screen in OPTIMIZABLE_SCREENS:
        _register_table_callback(app, db_path, screen)
        _register_run_callback(app, db_path, sector_risk_path, screen)


def _register_table_callback(app, db_path: str, screen):
    """Load optimized_parameters rows for this screen and render."""
    @app.callback(
        Output(f"bt-{screen.id}-table", "data"),
        Output(f"bt-{screen.id}-last-opt", "children"),
        Output(f"bt-{screen.id}-n-industries", "children"),
        Output(f"bt-{screen.id}-avg-ir", "children"),
        Output(f"bt-{screen.id}-status", "children"),
        Input("bt-auto-refresh", "n_intervals"),
    )
    def load_optimized(_n, _screen=screen):
        import sqlite3
        rows = []
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            db_rows = conn.execute("""
                SELECT * FROM optimized_parameters WHERE screen_id = ? ORDER BY industry
            """, (_screen.id,)).fetchall()
            for r in db_rows:
                rows.append(_format_optimized_row(dict(r), _screen.id))

        last_opt = "(never run)"
        if rows:
            dates = [r["last_optimized_at"] for r in rows if r.get("last_optimized_at")]
            if dates:
                last_opt = max(dates)[:16]

        n_ind = len(rows)
        irs = [r["information_ratio"] for r in rows
               if r.get("information_ratio") is not None]
        avg_ir = f"{sum(irs)/len(irs):+.3f}" if irs else "N/A"
        status = ("" if rows else
                  "No optimized parameters yet. Run: "
                  f"python main.py backtest optimize --screen {_screen.id}")
        return rows, last_opt, str(n_ind), avg_ir, status


def _register_run_callback(app, db_path: str, sector_risk_path: str, screen):
    """Click 'Run' to do a live backtest with default params, full universe, default window."""
    @app.callback(
        Output(f"bt-{screen.id}-run-result", "children"),
        Input(f"bt-{screen.id}-run-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def run_live_backtest(n_clicks, _screen=screen):
        if not n_clicks:
            return no_update
        engine = BacktestEngine(db_path, sector_risk_path=sector_risk_path)
        end = date_cls.today().strftime("%Y-%m-%d")
        try:
            result = engine.run(
                _screen, _screen.default_params,
                start_date="2020-01-01", end_date=end, rebalance_freq="quarterly",
                persist_repo=None,
            )
        except Exception as e:
            return html.Pre(f"ERROR: {e}", className="text-danger small")

        def pct(v): return f"{v*100:+.2f}%" if v is not None else "N/A"
        def num(v, d=3): return f"{v:.{d}f}" if v is not None else "N/A"

        return html.Div([
            html.P([html.Strong("Result: "),
                    f"{result.n_rebalances} rebalances, {result.n_unique_holdings} unique holdings"]),
            html.Table([
                html.Tr([html.Td(html.Strong("Total return")),
                         html.Td(pct(result.total_return))]),
                html.Tr([html.Td(html.Strong("Benchmark return")),
                         html.Td(pct(result.benchmark_return))]),
                html.Tr([html.Td(html.Strong("Excess")),
                         html.Td(pct(result.total_return - result.benchmark_return))]),
                html.Tr([html.Td(html.Strong("Information Ratio")),
                         html.Td(num(result.information_ratio))]),
                html.Tr([html.Td(html.Strong("Sharpe (per period)")),
                         html.Td(num(result.sharpe))]),
                html.Tr([html.Td(html.Strong("Max drawdown")),
                         html.Td(pct(result.max_drawdown))]),
                html.Tr([html.Td(html.Strong("Hit rate (beat bench)")),
                         html.Td(pct(result.hit_rate))]),
            ], className="table table-dark table-sm w-auto small"),
        ])


def _format_optimized_row(db_row: dict, screen_id: str) -> dict:
    """Decompose parameters_json and expose the headline columns."""
    try:
        params = json.loads(db_row.get("parameters_json") or "{}")
    except (TypeError, ValueError):
        params = {}

    out = {
        "industry": db_row.get("industry"),
        "information_ratio": _rnd(db_row.get("information_ratio"), 3),
        "n_walk_forward_windows": db_row.get("n_walk_forward_windows"),
        "last_optimized_at": (db_row.get("last_optimized_at") or "")[:16],
    }

    # Screen-specific param columns
    if screen_id == "value":
        out.update({
            "pe_max": _rnd(params.get("pe_max"), 1),
            "pb_max": _rnd(params.get("pb_max"), 1),
            "roe_min_pct": _rnd(_safe_pct(params.get("roe_min")), 0),
            "earnings_growth_min_pct": _rnd(_safe_pct(params.get("earnings_growth_min")), 0),
            "market_cap_min_b": _rnd(_safe_div(params.get("market_cap_min"), 1e9), 1),
        })
    elif screen_id == "quality_compounder":
        out.update({
            "roe_min_pct": _rnd(_safe_pct(params.get("roe_min")), 0),
            "de_max": _rnd(params.get("de_max"), 0),
            "earnings_growth_min_pct": _rnd(_safe_pct(params.get("earnings_growth_min")), 0),
            "market_cap_min_b": _rnd(_safe_div(params.get("market_cap_min"), 1e9), 1),
        })
    elif screen_id == "income":
        out.update({
            "dividend_yield_min": _rnd(params.get("dividend_yield_min"), 1),
            "market_cap_min_b": _rnd(_safe_div(params.get("market_cap_min"), 1e9), 1),
            "earnings_growth_min_pct": _rnd(_safe_pct(params.get("earnings_growth_min")), 0),
        })
    return out


def _rnd(v, n=1):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, n)


def _safe_pct(v):
    if v is None:
        return None
    try:
        return float(v) * 100
    except (TypeError, ValueError):
        return None


def _safe_div(v, d):
    if v is None:
        return None
    try:
        return float(v) / d
    except (TypeError, ValueError):
        return None
