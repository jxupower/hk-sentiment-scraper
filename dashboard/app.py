import dash
import dash_bootstrap_components as dbc

from dashboard.layout import build_layout
from dashboard.callbacks import register_callbacks
from dashboard.screener_callbacks import register_screener_callbacks
from dashboard.recommendations_callbacks import register_recommendations_callbacks
from dashboard.screens_callbacks import register_screens_callbacks
from dashboard.backtest_callbacks import register_backtest_callbacks
from dashboard.stock_research_callbacks import register_stock_research_callbacks
from dashboard.risk_callbacks import register_risk_callbacks
from dashboard.portfolio_callbacks import register_portfolio_callbacks
from dashboard import theme as T


def create_app(db_path: str, settings) -> dash.Dash:
    import config.settings as cfg

    watchlist = cfg.load_watchlist()
    sectors = list(watchlist.get("sectors", {}).keys())

    app = dash.Dash(
        __name__,
        external_stylesheets=[
            dbc.themes.FLATLY,
            "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap",
        ],
        suppress_callback_exceptions=True,
        title="HK & China Market Research",
    )

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

        /* Period-selector segmented control (Stock Research tab) */
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
    register_screener_callbacks(app, db_path)
    register_recommendations_callbacks(app, db_path)
    register_screens_callbacks(app, db_path)
    register_backtest_callbacks(app, db_path)
    register_stock_research_callbacks(app, db_path)
    register_risk_callbacks(app, db_path)
    register_portfolio_callbacks(app, db_path)

    return app
