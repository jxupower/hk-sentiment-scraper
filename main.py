import sys
from datetime import datetime

import click
from rich.console import Console
from rich.table import Table

console = Console()


def _build_components():
    """Instantiate all components and return a dict of them."""
    import config.settings as settings
    from storage.database import Database
    from storage.repository import (ArticleRepository, SentimentRepository,
                                     SignalRepository, SectorSignalRepository,
                                     SecuritiesRepository, FundamentalsRepository)
    from scrapers.rss_scraper import RssScraper
    from scrapers.yahoo_scraper import YahooScraper
    from scrapers.reddit_scraper import RedditScraper
    from analysis.sentiment import SentimentAnalyzer
    from analysis.signals import SignalGenerator, SectorSignalGenerator
    from scheduler.job_runner import JobRunner

    watchlist = settings.load_watchlist()
    all_tickers = settings.get_all_tickers(watchlist)
    search_terms = settings.build_search_terms(watchlist)
    rss_feeds = settings.load_rss_feeds()

    db = Database(settings.DB_PATH)
    db.initialize()

    article_repo = ArticleRepository(db)
    sentiment_repo = SentimentRepository(db)
    signal_repo = SignalRepository(db)
    sector_signal_repo = SectorSignalRepository(db)
    securities_repo = SecuritiesRepository(db)
    fundamentals_repo = FundamentalsRepository(db)

    _reconcile_universe_at_startup(securities_repo, watchlist, settings)

    yahoo = YahooScraper()
    scrapers = [
        RssScraper(rss_feeds),
        yahoo,
        RedditScraper(settings.REDDIT_CLIENT_ID, settings.REDDIT_CLIENT_SECRET,
                      settings.REDDIT_USER_AGENT),
    ]

    analyzer = SentimentAnalyzer(claude_api_key=settings.CLAUDE_API_KEY)
    signal_gen = SignalGenerator()
    sector_signal_gen = SectorSignalGenerator()

    runner = JobRunner(
        config=settings,
        scrapers=scrapers,
        analyzer=analyzer,
        signal_gen=signal_gen,
        sector_signal_gen=sector_signal_gen,
        article_repo=article_repo,
        sentiment_repo=sentiment_repo,
        signal_repo=signal_repo,
        sector_signal_repo=sector_signal_repo,
        securities_repo=securities_repo,
        fundamentals_repo=fundamentals_repo,
        yahoo_scraper=yahoo,
        search_terms=search_terms,
        all_tickers=all_tickers,
        watchlist=watchlist,
        interval_minutes=settings.SCRAPE_INTERVAL_MINUTES,
    )

    return {
        "settings": settings,
        "db": db,
        "article_repo": article_repo,
        "sentiment_repo": sentiment_repo,
        "signal_repo": signal_repo,
        "sector_signal_repo": sector_signal_repo,
        "securities_repo": securities_repo,
        "fundamentals_repo": fundamentals_repo,
        "runner": runner,
        "all_tickers": all_tickers,
        "watchlist": watchlist,
    }


def _reconcile_universe_at_startup(securities_repo, watchlist, settings):
    """Pull HKEX list (from cache if available) and reconcile into the securities table.

    Failure to reach HKEX (offline, network error) is non-fatal — we only need
    the watchlist subset for the existing scrape pipeline to keep working.
    """
    from universe import hkex_loader, reconciler
    cache_dir = settings.HKEX_CACHE_DIR
    today_cache = cache_dir / f"hkex_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    try:
        if today_cache.exists():
            records = hkex_loader.parse(today_cache)
        else:
            records = hkex_loader.download_and_parse(cache_dir)
        reconciler.reconcile(securities_repo, records, watchlist)
    except Exception as e:
        console.print(f"[yellow]Universe reconcile skipped (HKEX fetch failed: {e}).[/yellow]")
        console.print("[yellow]Falling back to watchlist-only mode.[/yellow]")
        try:
            reconciler.reconcile(securities_repo, [], watchlist)
        except Exception as e2:
            console.print(f"[red]Watchlist-only reconcile also failed: {e2}[/red]")


@click.group()
def cli():
    """Stock Sentiment Analysis Tool — scrapes news & social media to predict stock trends."""


@cli.command()
def setup():
    """Print setup instructions for optional API keys."""
    console.print("""
[bold cyan]Stock Sentiment Analysis Tool — Setup Guide[/bold cyan]

[bold]The tool works out of the box![/bold] RSS feeds and Yahoo Finance require no keys.
To unlock Reddit data and Claude AI-enhanced scoring, follow these steps:

[bold yellow]1. Reddit API (Free)[/bold yellow]
   a. Go to https://www.reddit.com/prefs/apps
   b. Click "Create App" → select "script"
   c. Name: SentimentScraper, Redirect URI: http://localhost:8080
   d. Copy the client_id (under app name) and client_secret
   e. Add to your .env file:
      REDDIT_CLIENT_ID=your_id
      REDDIT_CLIENT_SECRET=your_secret

[bold yellow]2. Claude API (Optional — improves sentiment accuracy)[/bold yellow]
   a. Go to https://console.anthropic.com
   b. Create an API key
   c. Add to your .env file:
      CLAUDE_API_KEY=your_key

[bold yellow]3. Create your .env file[/bold yellow]
   Copy .env.example to .env and fill in your credentials.

[bold green]Then run:[/bold green]
   python main.py scrape --once    # Test a single scrape
   python main.py dashboard        # Launch the dashboard at http://localhost:8050
""")


@cli.command()
@click.option("--once", is_flag=True, default=False, help="Run a single scrape cycle and exit.")
def scrape(once: bool):
    """Scrape news and social media, analyze sentiment, and update signals."""
    console.print("[bold cyan]Starting scraper...[/bold cyan]")
    components = _build_components()
    runner = components["runner"]

    if once:
        runner.run_once()
        console.print("[bold green]Scrape complete.[/bold green]")
        _print_sector_signals(components["sector_signal_repo"])
    else:
        import signal as sig
        import time
        runner.start()
        console.print(f"[green]Scraping every {components['settings'].SCRAPE_INTERVAL_MINUTES} minutes. Press Ctrl+C to stop.[/green]")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            runner.stop()
            console.print("\n[yellow]Scraper stopped.[/yellow]")


@cli.command()
@click.option("--port", default=8050, show_default=True, help="Port for the dashboard.")
@click.option("--debug", is_flag=True, default=False, help="Enable Dash debug mode.")
def dashboard(port: int, debug: bool):
    """Launch the web dashboard with background scraping at http://localhost:<port>."""
    components = _build_components()
    runner = components["runner"]
    runner.start()

    console.print(f"[bold cyan]Dashboard starting at http://localhost:{port}[/bold cyan]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]")

    try:
        from dashboard.app import create_app
        app = create_app(components["settings"].DB_PATH, components["settings"])
        app.run(host="0.0.0.0", port=port, debug=debug)
    except KeyboardInterrupt:
        runner.stop()
        console.print("\n[yellow]Dashboard stopped.[/yellow]")


@cli.group()
def universe():
    """Manage the HKEX-wide securities universe."""


@universe.command("refresh")
def universe_refresh():
    """Download the latest HKEX securities list and reconcile with watchlist.yaml."""
    import config.settings as settings
    from storage.database import Database
    from storage.repository import SecuritiesRepository
    from universe import hkex_loader, reconciler

    console.print("[bold cyan]Refreshing HKEX universe...[/bold cyan]")
    db = Database(settings.DB_PATH)
    db.initialize()
    securities_repo = SecuritiesRepository(db)

    watchlist = settings.load_watchlist()
    records = hkex_loader.download_and_parse(settings.HKEX_CACHE_DIR)
    summary = reconciler.reconcile(securities_repo, records, watchlist)

    table = Table(title="Universe Reconcile Summary", show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Total securities (rows)", str(summary["total"]))
    table.add_row("Watchlist tickers", str(summary["watchlist"]))
    table.add_row("HKEX rows ingested", str(summary["hkex_ingested"]))
    table.add_row("Watchlist tickers in YAML", str(summary["watchlist_in_yaml"]))
    table.add_row("Watchlist tickers missing from HKEX",
                  str(len(summary["missing_from_hkex"])))
    table.add_row("Deactivated (delisted)", str(summary.get("deactivated", 0)))
    console.print(table)
    if summary["missing_from_hkex"]:
        console.print(f"[yellow]Missing from HKEX (kept as overrides): "
                      f"{summary['missing_from_hkex']}[/yellow]")


@cli.group()
def fundamentals():
    """Manage the per-ticker fundamentals snapshot pipeline."""


@fundamentals.command("refresh")
@click.option("--tickers", default="WATCHLIST", show_default=True,
              help="ALL (full universe) | WATCHLIST | comma-separated tickers (e.g. '0700.HK,0005.HK')")
@click.option("--throttle", default=1.5, show_default=True, type=float,
              help="Seconds to sleep between yfinance requests.")
def fundamentals_refresh(tickers: str, throttle: float):
    """Pull yfinance .info ratios and write a daily snapshot per ticker."""
    import config.settings as settings
    from storage.database import Database
    from storage.repository import SecuritiesRepository, FundamentalsRepository
    from scrapers.fundamentals_scraper import FundamentalsScraper

    db = Database(settings.DB_PATH)
    db.initialize()
    securities_repo = SecuritiesRepository(db)
    fundamentals_repo = FundamentalsRepository(db)

    selector = tickers.strip().upper()
    if selector == "ALL":
        target = [s["ticker"] for s in securities_repo.get_universe()]
        console.print(f"[bold cyan]Refreshing fundamentals for ALL {len(target)} universe tickers...[/bold cyan]")
        console.print("[yellow]This will take ~1-2 hours. Press Ctrl+C to abort (already-fetched rows persist).[/yellow]")
    elif selector == "WATCHLIST":
        target = [s["ticker"] for s in securities_repo.get_watchlist()]
        console.print(f"[bold cyan]Refreshing fundamentals for {len(target)} watchlist tickers...[/bold cyan]")
    else:
        target = [t.strip() for t in tickers.split(",") if t.strip()]
        console.print(f"[bold cyan]Refreshing fundamentals for {len(target)} ticker(s): {target}[/bold cyan]")

    if not target:
        console.print("[red]No tickers to refresh. Did you run 'universe refresh' first?[/red]")
        return

    scraper = FundamentalsScraper(throttle_seconds=throttle)
    summary = scraper.fetch_many(target, fundamentals_repo)

    table = Table(title="Fundamentals Refresh Summary")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Attempted", str(summary["attempted"]))
    table.add_row("Written (new snapshot)", str(summary["written"]))
    table.add_row("Skipped (already had today's)", str(summary["skipped"]))
    table.add_row("Failed (yfinance error)", str(summary["failed"]))
    console.print(table)


@cli.group()
def historical():
    """Historical fundamentals (akshare) + multi-year price data (yfinance) for backtesting."""


@historical.command("seed")
@click.option("--tickers", default="WATCHLIST", show_default=True,
              help="ALL | WATCHLIST | comma-separated tickers")
@click.option("--throttle", default=0.5, show_default=True, type=float,
              help="Seconds between akshare requests.")
@click.option("--skip-prices", is_flag=True, default=False,
              help="Skip the yfinance price-history pull (only seed fundamentals).")
@click.option("--skip-fundamentals", is_flag=True, default=False,
              help="Skip the akshare fundamentals pull (only seed prices).")
@click.option("--price-period", default="10y", show_default=True,
              help="Period for yfinance history (e.g. 5y, 10y, max).")
def historical_seed(tickers, throttle, skip_prices, skip_fundamentals, price_period):
    """One-time backfill: pull akshare historical fundamentals + yfinance multi-year prices."""
    import config.settings as settings
    from storage.database import Database
    from storage.repository import (SecuritiesRepository, FundamentalsRepository,
                                     HistoricalPricesRepository)
    from scrapers.akshare_historical_scraper import fetch_many as ak_fetch_many
    from scrapers.historical_price_scraper import fetch_many as price_fetch_many

    db = Database(settings.DB_PATH)
    db.initialize()
    securities_repo = SecuritiesRepository(db)
    fundamentals_repo = FundamentalsRepository(db)
    prices_repo = HistoricalPricesRepository(db)

    selector = tickers.strip().upper()
    if selector == "ALL":
        target = [s["ticker"] for s in securities_repo.get_universe()]
        console.print(f"[bold cyan]Seeding historical data for ALL {len(target)} active securities...[/bold cyan]")
        console.print("[yellow]Fundamentals: ~6-8 hours at 0.5s throttle. Prices: ~10-15 min in batches.[/yellow]")
    elif selector == "WATCHLIST":
        target = [s["ticker"] for s in securities_repo.get_watchlist()]
        console.print(f"[bold cyan]Seeding historical data for {len(target)} watchlist tickers...[/bold cyan]")
    else:
        target = [t.strip() for t in tickers.split(",") if t.strip()]
        console.print(f"[bold cyan]Seeding historical data for {len(target)} ticker(s): {target}[/bold cyan]")

    if not target:
        console.print("[red]No tickers. Did you run 'universe refresh' first?[/red]")
        return

    if not skip_fundamentals:
        console.print(f"\n[bold]Stage A: akshare historical fundamentals (annual, ~9 years)[/bold]")
        ak_summary = ak_fetch_many(target, fundamentals_repo, securities_repo,
                                    throttle_seconds=throttle)
        console.print(f"  attempted: {ak_summary['attempted']}, "
                      f"snapshots_written: {ak_summary['snapshots_written']}, "
                      f"no_data: {ak_summary['no_data_tickers']}, "
                      f"failed: {ak_summary['failed_tickers']}")

    if not skip_prices:
        console.print(f"\n[bold]Stage B: yfinance multi-year price history (period={price_period})[/bold]")
        px_summary = price_fetch_many(target, prices_repo, period=price_period)
        console.print(f"  attempted: {px_summary['attempted']}, "
                      f"tickers_with_data: {px_summary['tickers_with_data']}, "
                      f"total_rows: {px_summary['total_rows']}, "
                      f"failed: {px_summary['failed_tickers']}")


@cli.group()
def backtest():
    """Backtest fundamental screens with walk-forward optimization."""


@backtest.command("run")
@click.option("--screen", "screen_id", required=True,
              help="Screen ID: value | quality_compounder | income")
@click.option("--start", default="2018-01-01", show_default=True,
              help="Start date (YYYY-MM-DD).")
@click.option("--end", default=None,
              help="End date (YYYY-MM-DD). Defaults to today.")
@click.option("--freq", default="quarterly", show_default=True,
              type=click.Choice(["monthly", "quarterly", "annual"]))
@click.option("--industry", default=None,
              help="Optional yf_sector filter (e.g. 'Financial Services').")
@click.option("--persist/--no-persist", default=True, show_default=True,
              help="Save run + holdings to DB.")
def backtest_run(screen_id, start, end, freq, industry, persist):
    """Run a single backtest of one screen with its default parameters."""
    from datetime import date as date_cls
    import config.settings as settings
    from storage.database import Database
    from storage.repository import BacktestRepository
    from analysis.backtest import BacktestEngine
    from analysis.screens import BUILTIN_SCREENS

    end = end or date_cls.today().strftime("%Y-%m-%d")
    screen = next((s for s in BUILTIN_SCREENS if s.id == screen_id), None)
    if not screen:
        console.print(f"[red]Unknown screen_id: {screen_id}. Options: "
                      f"{[s.id for s in BUILTIN_SCREENS]}[/red]")
        return

    db = Database(settings.DB_PATH)
    db.initialize()
    repo = BacktestRepository(db) if persist else None
    engine = BacktestEngine(settings.DB_PATH, sector_risk_path=str(
        settings.BASE_DIR / "config" / "sector_risk.yaml"))

    console.print(f"[bold cyan]Backtest: {screen.name} | freq={freq} | "
                  f"industry={industry or 'all'} | {start} to {end}[/bold cyan]")
    result = engine.run(screen, screen.default_params, start, end, rebalance_freq=freq,
                        industry_filter=industry, persist_repo=repo)

    def pct(v):
        return f"{v*100:+.2f}%" if v is not None else "N/A"
    def num(v, d=2):
        return f"{v:.{d}f}" if v is not None else "N/A"

    table = Table(title=f"Backtest Result — run_id={result.run_id}")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Rebalances", str(result.n_rebalances))
    table.add_row("Unique holdings", str(result.n_unique_holdings))
    table.add_row("Total return", pct(result.total_return))
    table.add_row("Benchmark return", pct(result.benchmark_return))
    table.add_row("Excess return", pct(result.total_return - result.benchmark_return))
    table.add_row("Information Ratio", num(result.information_ratio, 3))
    table.add_row("Sharpe (per-period)", num(result.sharpe, 3))
    table.add_row("Max drawdown", pct(result.max_drawdown))
    table.add_row("Hit rate (beat bench)", pct(result.hit_rate))
    console.print(table)


@backtest.command("optimize")
@click.option("--screen", "screen_id", required=True,
              help="Screen ID: value | quality_compounder | income")
@click.option("--industry", default=None,
              help="Optional: one yf_sector. Otherwise iterates all sectors with data.")
@click.option("--start", default="2018-01-01", show_default=True)
@click.option("--end", default=None, help="Defaults to today.")
@click.option("--train-months", default=36, show_default=True, type=int)
@click.option("--test-months", default=12, show_default=True, type=int)
@click.option("--step-months", default=12, show_default=True, type=int)
@click.option("--freq", default="quarterly", show_default=True,
              type=click.Choice(["monthly", "quarterly", "annual"]))
def backtest_optimize(screen_id, industry, start, end, train_months,
                       test_months, step_months, freq):
    """Walk-forward CV: find per-industry parameters that maximize Information Ratio."""
    from datetime import date as date_cls
    import sqlite3
    import config.settings as settings
    from storage.database import Database
    from storage.repository import OptimizedParamsRepository
    from analysis.optimization import WalkForwardOptimizer
    from analysis.screens import BUILTIN_SCREENS

    end = end or date_cls.today().strftime("%Y-%m-%d")
    screen = next((s for s in BUILTIN_SCREENS if s.id == screen_id), None)
    if not screen:
        console.print(f"[red]Unknown screen_id: {screen_id}[/red]")
        return

    db = Database(settings.DB_PATH)
    db.initialize()
    repo = OptimizedParamsRepository(db)

    # Determine industries to optimize
    if industry:
        industries = [industry]
    else:
        with sqlite3.connect(settings.DB_PATH) as conn:
            rows = conn.execute("""
                SELECT yf_sector, COUNT(DISTINCT ticker) AS n
                FROM securities WHERE is_active=1 AND yf_sector IS NOT NULL
                GROUP BY yf_sector HAVING n >= 5 ORDER BY yf_sector
            """).fetchall()
            industries = [r[0] for r in rows]

    console.print(f"[bold cyan]Optimizing {screen.name} across {len(industries)} industries[/bold cyan]")
    console.print(f"[dim]Range: {start} to {end} | train={train_months}mo + test={test_months}mo (step {step_months}mo)[/dim]")

    optimizer = WalkForwardOptimizer(
        settings.DB_PATH,
        sector_risk_path=str(settings.BASE_DIR / "config" / "sector_risk.yaml"),
    )
    results = optimizer.optimize_all_industries(
        screen, industries, start, end,
        train_window_months=train_months, test_window_months=test_months,
        step_months=step_months, rebalance_freq=freq, persist_repo=repo,
    )

    table = Table(title=f"Optimized Parameters for {screen.name}")
    table.add_column("Industry", style="bold")
    table.add_column("Avg IR", justify="right")
    table.add_column("Best params (key fields)")
    for r in results:
        p = r.best_params
        # Just show a few headline fields per screen
        if screen_id == "value":
            key = f"P/E≤{p.pe_max}  P/B≤{p.pb_max}  ROE≥{int(p.roe_min*100)}%  mc≥${p.market_cap_min/1e9:.0f}B"
        elif screen_id == "quality_compounder":
            key = f"ROE≥{int(p.roe_min*100)}%  D/E<{p.de_max}  eg≥{int(p.earnings_growth_min*100)}%  mc≥${p.market_cap_min/1e9:.0f}B"
        elif screen_id == "income":
            key = f"divY≥{p.dividend_yield_min}%  mc≥${p.market_cap_min/1e9:.0f}B  eg≥{int(p.earnings_growth_min*100)}%"
        else:
            key = "(see DB)"
        ir = f"{r.avg_information_ratio:+.3f}" if r.avg_information_ratio is not None else "N/A"
        table.add_row(r.industry, ir, key)
    console.print(table)


def _print_sector_signals(sector_signal_repo):
    signals = sector_signal_repo.get_latest_signals()
    if not signals:
        console.print("[yellow]No sector signals yet. Try running after scraping.[/yellow]")
        return

    table = Table(title="HK & China Market — Sector Signals", show_lines=True)
    table.add_column("Sector", style="bold")
    table.add_column("Direction")
    table.add_column("Sentiment 24h", justify="right")
    table.add_column("Sentiment 7d", justify="right")
    table.add_column("Articles 24h", justify="right")
    table.add_column("Avg Momentum", justify="right")
    table.add_column("Confidence", justify="right")

    DIR_COLORS = {"UP": "green", "DOWN": "red", "MIXED": "yellow", "NEUTRAL": "white"}
    for s in signals:
        color = DIR_COLORS.get(s["direction"], "white")
        table.add_row(
            s["sector"],
            f"[{color}]{s['direction']}[/{color}]",
            f"{s['avg_sentiment_24h']:.3f}" if s["avg_sentiment_24h"] is not None else "N/A",
            f"{s['avg_sentiment_7d']:.3f}" if s["avg_sentiment_7d"] is not None else "N/A",
            str(s["article_count_24h"]),
            f"{s['avg_price_momentum']:.2f}%" if s["avg_price_momentum"] is not None else "N/A",
            f"{s['confidence']:.0%}",
        )
    console.print(table)


@cli.group()
def audit():
    """Taxonomy / engine audits — generate markdown reports for human review."""


@audit.command("subsectors")
@click.option("--tail-pct", default=0.05, show_default=True, type=float,
              help="Tail size for V/Q/G outlier detection (top or bottom).")
def audit_subsectors(tail_pct):
    """Identify V/Q/G outliers per sub-sector and suggest reclassification."""
    from datetime import date as _date
    from analysis.audit_subsector_outliers import run_audit
    import config.settings as settings
    today = _date.today().isoformat()
    out = run_audit(
        db_path=settings.DB_PATH,
        sub_sectors_yaml="config/sub_sectors.yaml",
        output_md_path=f"data/audit_subsector_outliers_{today}.md",
        tail_pct=tail_pct,
    )
    console.print(f"[bold green]Audit written to[/bold green] {out}")


@cli.group()
def composites():
    """Sub-sector composite indices — `&NAME` synthetic tickers."""


@composites.command("rebuild")
@click.option("--sub-sector", default=None,
              help="Rebuild a single sub-sector (by its human label, "
                   "e.g. 'Banks'). Omit to rebuild every active sub-sector.")
def composites_rebuild(sub_sector):
    """Materialise sub-sector composite price series into historical_prices."""
    from analysis.subsector_synth import (
        rebuild_all_subsectors, rebuild_and_upsert_subsector,
    )
    import config.settings as settings
    from storage.database import Database
    db = Database(settings.DB_PATH)
    if sub_sector:
        summary = rebuild_and_upsert_subsector(sub_sector, db)
        console.print(summary)
    else:
        summary = rebuild_all_subsectors(db)
        console.print(
            f"[bold green]Rebuilt[/bold green] {summary['n_succeeded']}/"
            f"{summary['n_attempted']} composites · "
            f"{summary['total_rows_written']:,} rows · "
            f"{summary['elapsed_sec']:.1f}s"
        )
        if summary["errors"]:
            console.print(f"[bold yellow]Errors:[/bold yellow]")
            for e in summary["errors"][:10]:
                console.print(f"  {e}")


@composites.command("list")
def composites_list():
    """List every sub-sector with its derived composite ticker."""
    from analysis.subsector_synth import list_subsector_composites
    import config.settings as settings
    from storage.database import Database
    rows = list_subsector_composites(Database(settings.DB_PATH))
    table = Table(title=f"{len(rows)} sub-sector composites")
    table.add_column("Ticker", style="cyan")
    table.add_column("Sub-sector")
    table.add_column("Constituents", justify="right")
    for r in rows:
        table.add_row(r["ticker"], r["sub_sector"], str(r["n_constituents"]))
    console.print(table)


if __name__ == "__main__":
    cli()
