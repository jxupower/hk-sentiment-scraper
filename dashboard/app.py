import dash
import dash_bootstrap_components as dbc

from dashboard.layout import build_layout
from dashboard.callbacks import register_callbacks
from dashboard.screener_callbacks import register_screener_callbacks
from dashboard.recommendations_callbacks import register_recommendations_callbacks
from dashboard.screens_callbacks import register_screens_callbacks
from dashboard.backtest_callbacks import register_backtest_callbacks
from dashboard.stock_research_callbacks import register_stock_research_callbacks


def create_app(db_path: str, settings) -> dash.Dash:
    import config.settings as cfg

    watchlist = cfg.load_watchlist()
    sectors = list(watchlist.get("sectors", {}).keys())

    app = dash.Dash(
        __name__,
        external_stylesheets=[dbc.themes.DARKLY],
        suppress_callback_exceptions=True,
        title="HK & China Market Sentiment",
    )

    app.index_string = app.index_string.replace(
        "</head>",
        """<style>
        body { background: #0f0f23 !important; }
        .Select-control { background: #263238 !important; color: #eceff1 !important; }
        .Select-menu-outer { background: #263238 !important; }
        .Select-option { color: #eceff1 !important; }
        .Select-option:hover { background: #37474f !important; }
        </style></head>"""
    )

    from scrapers.yahoo_scraper import YahooScraper
    yahoo = YahooScraper()

    app.layout = build_layout(sectors)
    register_callbacks(app, db_path, cfg, watchlist, yahoo)
    register_screener_callbacks(app, db_path)
    register_recommendations_callbacks(app, db_path)
    register_screens_callbacks(app, db_path)
    register_backtest_callbacks(app, db_path)
    register_stock_research_callbacks(app, db_path)

    return app
