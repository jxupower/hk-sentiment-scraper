import dash
import dash_bootstrap_components as dbc

from dashboard.layout import build_layout
from dashboard.callbacks import register_callbacks


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
    register_callbacks(app, db_path, settings, watchlist, yahoo)

    return app
