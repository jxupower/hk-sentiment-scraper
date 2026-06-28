import dash
import dash_bootstrap_components as dbc

from dashboard.layout import build_layout
from dashboard.callbacks import register_callbacks
from dashboard.market_callbacks import register_market_callbacks
from dashboard.screener_callbacks import register_screener_callbacks
from dashboard.recommendations_callbacks import register_recommendations_callbacks
from dashboard.backtest_callbacks import register_backtest_callbacks
from dashboard.stock_research_callbacks import register_stock_research_callbacks
from dashboard.risk_callbacks import register_risk_callbacks
from dashboard.portfolio_callbacks import register_portfolio_callbacks
from dashboard import theme as T


def create_app(db_path: str, settings) -> dash.Dash:
    import config.settings as cfg

    watchlist = cfg.load_watchlist()
    # Sentiment tab buckets are sub-sectors now (post the 2026-06 watchlist-UI
    # removal — sentiment groups by sub_sector instead of the editorial
    # watchlist sector layer). We still draw from the watchlist roster
    # because those tickers carry rich alias coverage; the 75 universe-wide
    # sub-sectors would be too sparse for daily sentiment scoring.
    from storage.database import Database
    from storage.repository import SecuritiesRepository
    _db = Database(db_path)
    _db.initialize()
    _securities_repo = SecuritiesRepository(_db)
    sectors = cfg.get_subsectors_for_sentiment(watchlist, _securities_repo)

    # Pre-warm the Supabase connection pool at startup. The first query
    # otherwise pays a 500ms-2s TCP+auth handshake; doing it here moves
    # that cost out of every user's first click. Silent on local-only
    # configurations.
    if cfg.cloud_db_configured():
        try:
            from storage import cloud_db
            cloud_db.available()
            print("[dashboard] Supabase pool pre-warmed at startup")
        except Exception as e:
            print(f"[dashboard] Supabase pool pre-warm skipped: {e}")

    app = dash.Dash(
        __name__,
        external_stylesheets=[
            dbc.themes.FLATLY,
            "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap",
        ],
        suppress_callback_exceptions=True,
        title="Croissant Stock Analyser",
    )

    # --- Cloudflare Access pass-through ---------------------------------
    # When the dashboard runs behind Cloudflare Access (production deploy
    # per docs/deploy.md), every authenticated request carries the user's
    # email in the `Cf-Access-Authenticated-User-Email` header. Stash it
    # on `flask.g` so future per-user features (e.g. portfolios scoped to
    # the email, audit logging) can read it without re-parsing headers
    # at every call site.
    #
    # In local dev / CI the header is absent; `flask.g.user_email` stays
    # None and the app behaves as today (single-tenant). No auth is
    # enforced here — the gating happens at the Cloudflare edge before
    # the request ever reaches this process.
    #
    # Logged at INFO once per unique email so we have a record of who
    # used the dashboard without spamming on every callback fire.
    import logging
    from flask import g, request
    _seen_emails: set[str] = set()
    _auth_logger = logging.getLogger("dashboard.auth")

    @app.server.before_request
    def _capture_cf_access_email():
        email = request.headers.get("Cf-Access-Authenticated-User-Email")
        g.user_email = email
        if email and email not in _seen_emails:
            _seen_emails.add(email)
            _auth_logger.info("Cf-Access login: %s", email)
    # ---------------------------------------------------------------------

    # Custom CSS overrides: Inter typography, purple accent, light surfaces,
    # generous padding, soft shadows. All hex codes pull from theme.py — keep
    # them in lockstep via the f-string substitution.
    app.index_string = app.index_string.replace(
        "</head>",
        f"""<style>
        :root {{
            --bs-primary: {T.PRIMARY};
            --bs-info: {T.INFO};
            --bs-success: {T.SUCCESS};
            --bs-danger: {T.DANGER};
            --bs-warning: {T.WARNING};
        }}
        body {{
            background: {T.BG} !important;
            color: {T.TEXT} !important;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            font-feature-settings: "ss01", "cv11";
        }}

        /* Tabs: clean modern look with purple active indicator */
        .nav-tabs {{
            border-bottom: 1px solid {T.BORDER} !important;
        }}
        .nav-tabs .nav-link {{
            color: {T.TEXT_MUTED} !important;
            background: transparent !important;
            border: none !important;
            border-bottom: 2px solid transparent !important;
            padding: 12px 20px !important;
            font-weight: 500 !important;
            font-size: 0.95rem !important;
            transition: all 0.15s ease;
        }}
        .nav-tabs .nav-link:hover {{
            color: {T.PRIMARY} !important;
            border-bottom-color: {T.BORDER_STRONG} !important;
        }}
        .nav-tabs .nav-link.active {{
            color: {T.PRIMARY} !important;
            background: transparent !important;
            border-bottom: 2px solid {T.PRIMARY} !important;
            font-weight: 600 !important;
        }}

        /* Card defaults */
        .card {{
            background: {T.CARD_BG} !important;
            border: 1px solid {T.BORDER} !important;
            border-radius: 12px !important;
            box-shadow: {T.SHADOW_SM};
        }}
        .card-header {{
            background: transparent !important;
            border-bottom: 1px solid {T.BORDER} !important;
            color: {T.TEXT} !important;
            font-weight: 600 !important;
            padding: 14px 18px !important;
        }}
        .card-body {{
            padding: 18px !important;
        }}

        /* Buttons */
        .btn-primary {{
            background: {T.PRIMARY} !important;
            border-color: {T.PRIMARY} !important;
            font-weight: 500 !important;
            border-radius: 8px !important;
        }}
        .btn-primary:hover {{
            background: {T.PRIMARY_HOVER} !important;
            border-color: {T.PRIMARY_HOVER} !important;
        }}
        .btn {{
            border-radius: 8px !important;
            font-weight: 500 !important;
        }}

        /* Form inputs / dropdowns */
        .form-control, .form-select {{
            background: {T.CARD_BG} !important;
            color: {T.TEXT} !important;
            border: 1px solid {T.BORDER_STRONG} !important;
            border-radius: 8px !important;
        }}
        .form-control:focus, .form-select:focus {{
            border-color: {T.PRIMARY} !important;
            box-shadow: 0 0 0 3px {T.PRIMARY_SOFT} !important;
        }}

        /* Dash Dropdown (react-select based, needs different selectors) */
        .Select-control, .VirtualizedSelectFocusedOption {{
            background: {T.CARD_BG} !important;
            border-color: {T.BORDER_STRONG} !important;
            border-radius: 8px !important;
        }}
        .Select-menu-outer {{
            background: {T.CARD_BG} !important;
            border-color: {T.BORDER_STRONG} !important;
            border-radius: 8px !important;
            box-shadow: {T.SHADOW_MD};
        }}
        .Select-option {{
            color: {T.TEXT} !important;
            background: {T.CARD_BG} !important;
        }}
        .Select-option.is-focused {{
            background: {T.PRIMARY_SOFT} !important;
            color: {T.PRIMARY} !important;
        }}
        .Select-value-label {{
            color: {T.TEXT} !important;
        }}
        .Select-placeholder {{
            color: {T.TEXT_FAINT} !important;
        }}

        /* Slider */
        .rc-slider-track {{
            background: {T.PRIMARY} !important;
        }}
        .rc-slider-handle {{
            border-color: {T.PRIMARY} !important;
            background: {T.PRIMARY} !important;
        }}
        .rc-slider-rail {{
            background: {T.BORDER_STRONG} !important;
        }}

        /* Period-selector segmented control (Stock Research / Risk / Portfolio tabs) */
        /* Override Bootstrap's btn-group flex-wrap: nowrap so buttons wrap to a
           second line instead of overflowing horizontally into adjacent columns
           when the parent column is squeezed. Small gap so wrapped rows have
           breathing room. */
        .sr-period-radio {{
            flex-wrap: wrap !important;
            gap: 2px !important;
            max-width: 100% !important;
        }}
        .sr-period-radio label.btn-outline-primary {{
            color: {T.TEXT_MUTED} !important;
            border-color: {T.BORDER_STRONG} !important;
            background: {T.CARD_BG} !important;
            font-weight: 600 !important;
            font-size: 0.78rem !important;
            padding: 4px 12px !important;
        }}
        .sr-period-radio label.btn-outline-primary:hover {{
            background: {T.PRIMARY_SOFT} !important;
            color: {T.PRIMARY} !important;
            border-color: {T.PRIMARY} !important;
        }}
        .sr-period-radio label.btn-outline-primary.active,
        .sr-period-radio .btn-check:checked + label.btn-outline-primary {{
            background: {T.PRIMARY} !important;
            color: white !important;
            border-color: {T.PRIMARY} !important;
            box-shadow: {T.SHADOW_SM} !important;
        }}

        /* Badge colors stay semantic but with our palette */
        .badge.bg-primary {{ background: {T.PRIMARY} !important; }}
        .badge.bg-success {{ background: {T.SUCCESS} !important; }}
        .badge.bg-danger  {{ background: {T.DANGER} !important; }}
        .badge.bg-warning {{ background: {T.WARNING} !important; color: white !important; }}
        .badge.bg-info    {{ background: {T.INFO} !important; }}

        /* DataTable text */
        .dash-table-container .dash-spreadsheet-container .dash-spreadsheet-inner table {{
            font-family: 'Inter', sans-serif !important;
        }}

        /* Alert tweaks: subtle, light tinted */
        .alert {{
            border-radius: 10px !important;
            border-width: 1px !important;
        }}
        .alert-info {{
            background: {T.INFO_SOFT} !important;
            color: #075985 !important;
            border-color: #bae6fd !important;
        }}
        .alert-warning {{
            background: {T.WARNING_SOFT} !important;
            color: #92400e !important;
            border-color: #fde68a !important;
        }}

        /* Custom utility classes */
        .hero-number {{
            font-size: {T.FONT_HERO} !important;
            font-weight: 700 !important;
            color: {T.TEXT} !important;
            line-height: 1.1 !important;
            letter-spacing: -0.02em !important;
        }}
        .hero-number-lg {{
            font-size: {T.FONT_HERO_LG} !important;
            font-weight: 800 !important;
            color: {T.TEXT} !important;
            line-height: 1.1 !important;
            letter-spacing: -0.02em !important;
        }}
        .stat-label {{
            font-size: 0.75rem !important;
            font-weight: 500 !important;
            color: {T.TEXT_MUTED} !important;
            text-transform: uppercase !important;
            letter-spacing: 0.06em !important;
        }}

        /* Tighten Plotly tooltip styling */
        .plotly .hovertext {{
            font-family: 'Inter', sans-serif !important;
        }}
        </style></head>"""
    )

    from scrapers.yahoo_scraper import YahooScraper
    yahoo = YahooScraper()

    app.layout = build_layout(sectors)
    register_callbacks(app, db_path, cfg, watchlist, yahoo)
    register_market_callbacks(app, db_path)
    register_screener_callbacks(app, db_path)
    register_recommendations_callbacks(app, db_path)
    register_backtest_callbacks(app, db_path)
    register_stock_research_callbacks(app, db_path)
    register_risk_callbacks(app, db_path)
    register_portfolio_callbacks(app, db_path)

    return app
