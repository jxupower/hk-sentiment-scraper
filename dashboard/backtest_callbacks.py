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
from dashboard.charts import (
    drawdown_curve_chart,
    equity_curve_chart,
    sector_breakdown_chart,
)
from dashboard.screener_presets import INVESTOR_PRESETS

logger = logging.getLogger(__name__)

YEARS_TO_DAYS = 365


def register_backtest_callbacks(app, db_path: str):
    sector_risk_path = os.path.join(os.path.dirname(__file__), "..", "config",
                                     "sector_risk.yaml")

    # ----- i18n: flip every translatable label on language change -----
    @app.callback(
        Output("bt-setup-title", "children"),
        Output("bt-label-preset", "children"),
        Output("bt-label-horizon", "children"),
        Output("bt-label-rebal", "children"),
        Output("bt-label-weight-cap", "children"),
        Output("bt-preset-select", "options"),
        Output("bt-horizon-select", "options"),
        Output("bt-rebal-select", "options"),
        Output("bt-perf-title", "children"),
        Output("bt-stat-total-label", "children"),
        Output("bt-stat-annret-label", "children"),
        Output("bt-stat-vol-label", "children"),
        Output("bt-stat-sharpe-label", "children"),
        Output("bt-stat-maxdd-label", "children"),
        Output("bt-stat-hit-label", "children"),
        Output("bt-stat-excess-label", "children"),
        Output("bt-stat-turnover-label", "children"),
        Output("bt-section-equity", "children"),
        Output("bt-section-drawdown", "children"),
        Output("bt-section-sector", "children"),
        Output("bt-section-initial", "children"),
        Output("bt-section-changes", "children"),
        Output("bt-section-final", "children"),
        Output("bt-initial-table", "columns"),
        Output("bt-trades-table", "columns"),
        Output("bt-final-table", "columns"),
        Output("bt-save-title", "children"),
        Output("bt-save-subtitle", "children"),
        Output("bt-save-btn", "children"),
        Output("bt-run-btn", "children", allow_duplicate=True),
        Input("user-language", "data"),
        prevent_initial_call="initial_duplicate",
    )
    def i18n_backtest(lang):
        from dashboard.i18n import T as I
        lang = lang or "en"
        preset_options = [{"label": I(f"screener.preset.{p['id']}.label", lang),
                             "value": p["id"]}
                            for p in INVESTOR_PRESETS]
        horizon_options = [
            {"label": I("backtest.horizon.1y", lang), "value": 1},
            {"label": I("backtest.horizon.3y", lang), "value": 3},
            {"label": I("backtest.horizon.5y", lang), "value": 5},
        ]
        rebal_options = [
            {"label": I("backtest.rebal.1d", lang), "value": "1d"},
            {"label": I("backtest.rebal.3d", lang), "value": "3d"},
            {"label": I("backtest.rebal.1w", lang), "value": "1w"},
            {"label": I("backtest.rebal.1m", lang), "value": "1m"},
        ]
        initial_cols = [
            {"name": I("backtest.col.ticker", lang),  "id": "ticker"},
            {"name": I("backtest.col.name", lang),    "id": "name"},
            {"name": I("backtest.col.price", lang),   "id": "price",
             "type": "numeric"},
            {"name": I("backtest.col.weight", lang),  "id": "weight",
             "type": "numeric"},
            {"name": I("backtest.col.shares", lang),  "id": "shares",
             "type": "numeric"},
        ]
        trades_cols = [
            {"name": I("backtest.col.date", lang),    "id": "date"},
            {"name": I("backtest.col.ticker", lang),  "id": "ticker"},
            {"name": I("backtest.col.name", lang),    "id": "name"},
            {"name": I("backtest.col.action", lang),  "id": "action"},
            {"name": I("backtest.col.units", lang),   "id": "units",
             "type": "numeric"},
            {"name": I("backtest.col.price", lang),   "id": "price",
             "type": "numeric"},
        ]
        final_cols = [
            {"name": I("backtest.col.ticker", lang),       "id": "ticker"},
            {"name": I("backtest.col.name", lang),         "id": "name"},
            {"name": I("backtest.col.price", lang),        "id": "price",
             "type": "numeric"},
            {"name": I("backtest.col.weight", lang),       "id": "weight",
             "type": "numeric"},
            {"name": I("backtest.col.weight_delta", lang), "id": "weight_delta",
             "type": "numeric"},
            {"name": I("backtest.col.shares", lang),       "id": "shares",
             "type": "numeric"},
            {"name": I("backtest.col.shares_delta", lang), "id": "shares_delta",
             "type": "numeric"},
        ]
        return (
            I("backtest.setup", lang),
            I("backtest.label.preset", lang),
            I("backtest.label.horizon", lang),
            I("backtest.label.rebal", lang),
            I("backtest.label.weight_cap", lang),
            preset_options,
            horizon_options,
            rebal_options,
            I("backtest.performance", lang),
            I("backtest.stat.total", lang),
            I("backtest.stat.annret", lang),
            I("backtest.stat.vol", lang),
            I("backtest.stat.sharpe", lang),
            I("backtest.stat.maxdd", lang),
            I("backtest.stat.hit", lang),
            I("backtest.stat.excess", lang),
            I("backtest.stat.turnover", lang),
            I("backtest.section.equity", lang),
            I("backtest.section.drawdown", lang),
            I("backtest.section.sector", lang),
            I("backtest.section.initial", lang),
            I("backtest.section.changes", lang),
            I("backtest.section.final", lang),
            initial_cols, trades_cols, final_cols,
            I("backtest.save_title", lang),
            I("backtest.save_sub", lang),
            I("backtest.btn.save", lang),
            I("backtest.btn.run", lang),
        )

    # --- Run-button feedback (clientside, fires instantly on click) ----
    # Disables the button + swaps the label so users don't double-click.
    # The label needs the current language read from the user-language Store
    # — we read it inline in JS, no Python round-trip.
    app.clientside_callback(
        """
        function(n_clicks, lang) {
            if (!n_clicks) {
                return window.dash_clientside.no_update;
            }
            const running = (lang === "zh") ? "运行中... (约 30-60 秒)"
                                              : "Running... (~30-60s)";
            return [true, running];
        }
        """,
        Output("bt-run-btn", "disabled", allow_duplicate=True),
        Output("bt-run-btn", "children", allow_duplicate=True),
        Input("bt-run-btn", "n_clicks"),
        State("user-language", "data"),
        prevent_initial_call=True,
    )

    # --- Run backtest -------------------------------------------------
    @app.callback(
        Output("bt-stat-total", "children"),
        Output("bt-stat-annret", "children"),
        Output("bt-stat-vol", "children"),
        Output("bt-stat-sharpe", "children"),
        Output("bt-stat-maxdd", "children"),
        Output("bt-stat-hit", "children"),
        Output("bt-stat-excess", "children"),
        Output("bt-stat-turnover", "children"),
        Output("bt-window-label", "children"),
        Output("bt-equity-chart", "figure"),
        Output("bt-drawdown-chart", "figure"),
        Output("bt-sector-initial", "figure"),
        Output("bt-sector-final", "figure"),
        Output("bt-initial-table", "data"),
        Output("bt-initial-rebal-date", "children"),
        Output("bt-trades-table", "data"),
        Output("bt-trades-summary", "children"),
        Output("bt-final-table", "data"),
        Output("bt-final-rebal-date", "children"),
        Output("bt-survivors-store", "data"),
        Output("bt-preset-label-store", "data"),
        Output("bt-save-preview", "children"),
        Output("bt-save-btn", "disabled"),
        Output("bt-run-status", "children"),
        Output("bt-run-btn", "disabled", allow_duplicate=True),
        Output("bt-run-btn", "children", allow_duplicate=True),
        Input("bt-run-btn", "n_clicks"),
        State("bt-preset-select", "value"),
        State("bt-horizon-select", "value"),
        State("bt-rebal-select", "value"),
        State("bt-weight-cap", "value"),
        State("user-language", "data"),
        prevent_initial_call=True,
    )
    def run_backtest(_n, preset_id, horizon_years, rebal_freq, weight_cap, lang):
        from dashboard.i18n import T as I
        lang = lang or "en"
        run_btn_text = I("backtest.btn.run", lang)
        if not preset_id or not horizon_years or not rebal_freq:
            raise PreventUpdate
        # Anchor start to today − N years; the engine snaps to the nearest
        # ^HSI trading day on-or-after via the trading-day calendar.
        from datetime import date, timedelta
        end_iso = date.today().isoformat()
        start_iso = (date.today() -
                     timedelta(days=int(horizon_years) * YEARS_TO_DAYS)).isoformat()
        cap = float(weight_cap or 0.20)

        # Build the 'failure' return tuple inline so we don't drift out of
        # sync with the Outputs list — same length, sane defaults.
        def _fail(msg):
            return ("—",) * 8 + ("",) + ({},) * 4 + ([], "", [], "", [], "",
                                                       [], "", "", True, msg,
                                                       False, run_btn_text)

        t0 = time.time()
        try:
            result = run_preset_backtest(
                preset_id, start_iso, end_iso, rebal_freq,
                db_path, sector_risk_path, weight_cap=cap,
            )
        except Exception as e:
            logger.exception("Backtest failed")
            return _fail(f"Failed: {e}")
        elapsed = time.time() - t0

        m = result.metrics
        total_pct    = f"{m.total_return * 100:+.1f}%"
        annret_pct   = f"{m.annualized_return * 100:+.1f}%"
        vol_pct      = f"{m.annualized_vol * 100:.1f}%"
        sharpe_str   = f"{m.sharpe:+.2f}"
        maxdd_pct    = f"{m.max_drawdown * 100:.1f}%"
        hit_pct      = f"{m.hit_rate * 100:.0f}%"
        excess_pct   = f"{m.excess_return * 100:+.1f}%"
        turnover_pct = f"{m.annualized_turnover * 100:.0f}%"

        window_label = (f"{result.actual_start} → {result.actual_end}  "
                         f"·  cap {result.weight_cap_used*100:.0f}%  "
                         f"·  {m.n_rebalances} rebalances")

        # Prefer the smoother daily curves when populated; fall back to
        # rebalance-only samples (e.g. when price_cache is sparse).
        eq_data = result.daily_equity_curve or result.equity_curve
        bench_data = result.daily_benchmark_curve or result.benchmark_curve
        fig = equity_curve_chart(
            eq_data, bench_data,
            strategy_label=f"{result.preset_label} top-10",
        )
        dd_fig = drawdown_curve_chart(result.drawdown_curve)
        sec_init_fig = sector_breakdown_chart(
            result.sector_breakdown_initial, title="Sector mix — initial",
        )
        sec_final_fig = sector_breakdown_chart(
            result.sector_breakdown_final, title="Sector mix — final",
        )

        # Initial holdings keyed for the delta lookup on the final table.
        initial_by_ticker = {h.ticker: h
                              for h in result.rebalance_log[0].holdings}

        def _row(h):
            return {"ticker": h.ticker, "name": h.name,
                    "price": round(h.price, 2) if h.price else None,
                    "weight": round(h.weight * 100, 2),
                    "shares": round(h.shares, 2)}

        def _final_row(h):
            init = initial_by_ticker.get(h.ticker)
            init_w = init.weight if init else 0.0
            init_sh = init.shares if init else 0.0
            return {"ticker": h.ticker, "name": h.name,
                    "price": round(h.price, 2) if h.price else None,
                    "weight": round(h.weight * 100, 2),
                    "weight_delta": round((h.weight - init_w) * 100, 2),
                    "shares": round(h.shares, 2),
                    "shares_delta": round(h.shares - init_sh, 2)}

        initial_holdings = sorted(result.rebalance_log[0].holdings,
                                    key=lambda h: -h.weight)
        initial_rows = [_row(h) for h in initial_holdings]
        initial_date = (f"as of {result.rebalance_log[0].date} "
                          f"— notional capital HK$1,000,000")

        final_holdings = sorted(result.rebalance_log[-1].holdings,
                                  key=lambda h: -h.weight)
        final_rows = [_final_row(h) for h in final_holdings]
        final_date = (f"as of {result.rebalance_log[-1].date} — "
                      f"{m.n_rebalances} rebalances in {elapsed:.1f}s "
                      f"· Δ vs initial in colour")

        trade_rows = [{"date": tr.date, "ticker": tr.ticker, "name": tr.name,
                       "action": tr.action, "units": tr.units,
                       "price": round(tr.price, 2) if tr.price else None}
                      for tr in result.trade_log]
        n_buy = sum(1 for tr in result.trade_log if tr.action == "BUY")
        n_sell = sum(1 for tr in result.trade_log if tr.action == "SELL")
        total_vol = sum(tr.units * (tr.price or 0) for tr in result.trade_log)
        biggest = max(result.trade_log,
                       key=lambda tr: tr.units * (tr.price or 0),
                       default=None)
        biggest_str = (f"biggest: {biggest.action} {biggest.ticker} "
                        f"HK${biggest.units * biggest.price:,.0f}"
                        if biggest else "")
        trades_summary = (
            f"{len(result.trade_log)} trades — {n_buy} buys, {n_sell} sells "
            f"· total volume HK${total_vol:,.0f}  ·  {biggest_str}"
        )

        n_surv = len(result.preset_survivors_at_start)
        if n_surv > 0:
            preview_name = result.next_portfolio_name or "(name unavailable)"
            save_preview = (
                f"Will save as '{preview_name}' — {n_surv} preset survivor"
                f"{'s' if n_surv != 1 else ''} at {result.rebalance_log[0].date}, "
                f"100 shares each, rf = 3%."
            )
        else:
            save_preview = "No preset survivors at start date — nothing to save."
        run_status = (f"Ran {m.n_rebalances} rebalances in {elapsed:.1f}s. "
                       f"Top-10 from {n_surv}-ticker survivor set.")

        return (total_pct, annret_pct, vol_pct, sharpe_str, maxdd_pct,
                 hit_pct, excess_pct, turnover_pct, window_label,
                 fig, dd_fig, sec_init_fig, sec_final_fig,
                 initial_rows, initial_date,
                 trade_rows, trades_summary,
                 final_rows, final_date,
                 result.preset_survivors_at_start, result.preset_label,
                 save_preview, n_surv == 0, run_status,
                 False, "Run backtest")

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
