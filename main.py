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
    table.add_row("Total securities (active)", str(summary["total"]))
    table.add_row("Watchlist tickers", str(summary["watchlist"]))
    table.add_row("HKEX rows ingested", str(summary["hkex_ingested"]))
    table.add_row("Watchlist tickers in YAML", str(summary["watchlist_in_yaml"]))
    table.add_row("Watchlist tickers missing from HKEX",
                  str(len(summary["missing_from_hkex"])))
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


if __name__ == "__main__":
    cli()
