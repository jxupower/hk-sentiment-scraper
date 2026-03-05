import sys
import click
from rich.console import Console
from rich.table import Table

console = Console()


def _build_components():
    """Instantiate all components and return a dict of them."""
    import config.settings as settings
    from storage.database import Database
    from storage.repository import (ArticleRepository, SentimentRepository,
                                     SignalRepository, SectorSignalRepository)
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
        "runner": runner,
        "all_tickers": all_tickers,
        "watchlist": watchlist,
    }


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
