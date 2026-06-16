"""Backtest tab callbacks — preset + V/Q/G top-10 walk-forward.

Two callbacks register here:
  1. `run_backtest` — fires on the Run button. Calls
     `analysis.factor_backtest.run_preset_backtest()` and populates stats,
     equity-curve chart, holdings table, and the survivors `dcc.Store`
     that backs the Save button.
  2. `save_backtest_portfolio` — fires on Save. Picks the next free
     "<Strategy> backtest #N" name, writes a 100-share-each portfolio to
     Supabase via `CloudPortfoliosRepository`, then writes a cross-tab-nav
     payload that the Portfolio tab listens for to auto-load the new entry.

The previous per-screen sub-tab UI was removed in this overhaul. The CLI
commands `python main.py backtest run|optimize` still work against the
legacy engine.
"""
import logging
import os
import time

from dash import Input, Output, State, no_update
from dash.exceptions import PreventUpdate

from analysis.factor_backtest import (
    next_backtest_portfolio_name,
    run_preset_backtest,
)
from dashboard.charts import equity_curve_chart
from dashboard.screener_presets import INVESTOR_PRESETS

logger = logging.getLogger(__name__)

YEARS_TO_DAYS = 365


def register_backtest_callbacks(app, db_path: str):
    sector_risk_path = os.path.join(os.path.dirname(__file__), "..", "config",
                                     "sector_risk.yaml")

    # --- Run backtest -------------------------------------------------
    @app.callback(
        Output("bt-stat-total", "children"),
        Output("bt-stat-annret", "children"),
        Output("bt-stat-vol", "children"),
        Output("bt-stat-sharpe", "children"),
        Output("bt-stat-maxdd", "children"),
        Output("bt-stat-hit", "children"),
        Output("bt-equity-chart", "figure"),
        Output("bt-holdings-table", "data"),
        Output("bt-final-rebal-date", "children"),
        Output("bt-survivors-store", "data"),
        Output("bt-preset-label-store", "data"),
        Output("bt-save-preview", "children"),
        Output("bt-save-btn", "disabled"),
        Output("bt-run-status", "children"),
        Input("bt-run-btn", "n_clicks"),
        State("bt-preset-select", "value"),
        State("bt-horizon-select", "value"),
        State("bt-rebal-select", "value"),
        prevent_initial_call=True,
    )
    def run_backtest(_n, preset_id, horizon_years, rebal_freq):
        if not preset_id or not horizon_years or not rebal_freq:
            raise PreventUpdate
        # Anchor start to today − N years; the engine snaps to the nearest
        # ^HSI trading day on-or-after via the trading-day calendar.
        from datetime import date, timedelta
        end_iso = date.today().isoformat()
        start_iso = (date.today() -
                     timedelta(days=int(horizon_years) * YEARS_TO_DAYS)).isoformat()

        t0 = time.time()
        try:
            result = run_preset_backtest(
                preset_id, start_iso, end_iso, rebal_freq,
                db_path, sector_risk_path,
            )
        except Exception as e:
            logger.exception("Backtest failed")
            return ("—", "—", "—", "—", "—", "—",
                     {}, [], "", [], "",
                     "", True, f"Failed: {e}")
        elapsed = time.time() - t0

        m = result.metrics
        total_pct  = f"{m.total_return * 100:+.1f}%"
        annret_pct = f"{m.annualized_return * 100:+.1f}%"
        vol_pct    = f"{m.annualized_vol * 100:.1f}%"
        sharpe_str = f"{m.sharpe:+.2f}"
        maxdd_pct  = f"{m.max_drawdown * 100:.1f}%"
        hit_pct    = f"{m.hit_rate * 100:.0f}%"

        fig = equity_curve_chart(
            result.equity_curve, result.benchmark_curve,
            strategy_label=f"{result.preset_label} top-10",
        )

        # Final-rebalance holdings, sorted by weight desc, weight in %
        final_holdings = sorted(result.rebalance_log[-1].holdings,
                                 key=lambda h: -h[1])
        holdings_rows = [{"ticker": t, "weight": round(w * 100, 2)}
                         for t, w in final_holdings]
        final_date = (f"as of {result.rebalance_log[-1].date} — "
                      f"{m.n_rebalances} rebalances in {elapsed:.1f}s")

        n_surv = len(result.preset_survivors_at_start)
        save_preview = (
            f"Will save {n_surv} ticker{'s' if n_surv != 1 else ''} "
            f"(preset survivors at {result.rebalance_log[0].date}) "
            f"× 100 shares each, rf = 3%."
            if n_surv > 0 else
            "No preset survivors at start date — nothing to save."
        )
        run_status = (f"Ran {m.n_rebalances} rebalances in {elapsed:.1f}s. "
                       f"Top-10 from {n_surv}-ticker survivor set.")

        return (total_pct, annret_pct, vol_pct, sharpe_str, maxdd_pct,
                 hit_pct, fig, holdings_rows, final_date,
                 result.preset_survivors_at_start, result.preset_label,
                 save_preview, n_surv == 0, run_status)

    # --- Save to portfolio --------------------------------------------
    @app.callback(
        Output("bt-save-status", "children"),
        Output("main-tabs", "active_tab", allow_duplicate=True),
        Output("cross-tab-nav", "data", allow_duplicate=True),
        Input("bt-save-btn", "n_clicks"),
        State("bt-survivors-store", "data"),
        State("bt-preset-label-store", "data"),
        prevent_initial_call=True,
    )
    def save_backtest_portfolio(_n, survivors, preset_label):
        if not survivors or not preset_label:
            raise PreventUpdate
        try:
            from analysis.portfolio_synth import rebuild_and_upsert
            from storage.cloud_repository import CloudPortfoliosRepository
            from storage.database import Database
            repo = CloudPortfoliosRepository()
            name = next_backtest_portfolio_name(preset_label, repo)
            holdings = [{"ticker": t, "shares": 100} for t in survivors]
            repo.save_portfolio(
                name, holdings,
                optimal_weights=None,
                rf=0.03,
                weight_cap=0.30,
                lookback_days=None,
                notes=f"Auto-saved from Backtest tab: {preset_label} preset survivors.",
            )
            # Materialise the @NAME synthetic ticker so the saved portfolio
            # is renderable on Risk Forecast etc. immediately. Mirrors the
            # behaviour of the Portfolio tab's save_status_quo flow.
            portfolio_dict = {"name": name, "holdings": holdings,
                                "optimal_weights": None}
            rebuild_and_upsert(name, portfolio_dict, Database(db_path))
        except Exception as e:
            logger.exception("Save backtest portfolio failed")
            return f"Save failed: {e}", no_update, no_update

        nav = {"tab": "tab-portfolio", "portfolio": name,
               "ts": int(time.time() * 1000)}
        return (f"Saved as '{name}' — switching to Portfolio tab.",
                 "tab-portfolio", nav)
