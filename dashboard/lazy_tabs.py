"""Lazy tab materialisation (perf P2.8).

`dashboard/layout.py:build_tabs` starts every non-active tab as an empty
`dcc.Loading` placeholder. This module registers one callback per tab
that fills its placeholder when the user clicks the tab. The tab's
inner Dash callbacks (Screener updates, Research render, etc) only
start firing once the placeholder is populated — cuts the cold `/`
callback storm from ~30 (all 7 tabs' worth) to ~5 (Market only).

Behaviour:
  - First activation of a tab: builder runs, placeholder fills, all
    the tab's normal callbacks fire against the freshly-mounted DOM.
    User sees the dcc.Loading spinner for ~200-800 ms.
  - Subsequent activations within the session: placeholder already
    populated, callback returns PreventUpdate immediately (no rebuild).
  - Market is always eagerly built by `build_tabs`, so the landing
    page has real content on first paint.
  - Sentiment tab needs the `sectors` list, so it's the one special
    case where the builder is `None` in TAB_DEFS and we call the
    module-private `_sentiment_tab(sectors)` helper.

`suppress_callback_exceptions=True` (already set in create_app) is
what makes this safe: Dash allows callbacks whose Inputs/Outputs
target components that don't yet exist in the DOM. Those callbacks
are queued and fire the moment their Output component is mounted —
which is exactly what happens when a lazy tab first activates.
"""
from __future__ import annotations

from dash import Input, Output, State
from dash.exceptions import PreventUpdate

from dashboard.layout import TAB_DEFS, _sentiment_tab


def register_lazy_tab_callbacks(app, sectors: list[str],
                                  *, eager_tab_id: str = "tab-market") -> None:
    """Register one lazy-load callback per non-eager tab.

    Called once from create_app after all other callbacks are registered.
    `sectors` is threaded in because the Sentiment tab builder needs it
    (its builder in TAB_DEFS is None; we call the module-private helper
    directly, matching what build_tabs does for the eager path).
    """
    for _key, tab_id, builder in TAB_DEFS:
        if tab_id == eager_tab_id:
            continue                # eagerly materialised by build_tabs

        # Bind loop vars into the closure by default-argument capture.
        @app.callback(
            Output(f"{tab_id}-content", "children"),
            Input("main-tabs", "active_tab"),
            State(f"{tab_id}-content", "children"),
            prevent_initial_call=True,
        )
        def _lazy_load(active, existing,
                        _tab_id=tab_id, _builder=builder):
            if active != _tab_id:
                raise PreventUpdate
            # `existing` is the current children of the dcc.Loading. If
            # it's already populated (a list of real components) we've
            # loaded this tab before in the session — no rebuild.
            if existing:
                raise PreventUpdate
            if _builder is None:
                return _sentiment_tab(sectors)
            return _builder()
