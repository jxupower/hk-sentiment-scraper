import os
import sys
import time
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
    """Croissant Stock Analyser — sentiment + fundamentals + backtest over HK + US markets."""
    # Windows defaults stdout to cp1252; reconfigure so non-ASCII log lines
    # (e.g. Chinese sub-sector labels) don't crash the CLI with UnicodeEncodeError.
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


@cli.command()
def setup():
    """Print setup instructions for optional API keys."""
    console.print("""
[bold cyan]Croissant Stock Analyser — Setup Guide[/bold cyan]

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
@click.option("--market", default="HK", show_default=True,
              type=click.Choice(["HK", "US", "ALL"]),
              help="HK / US / ALL — which market's feeds + watchlist to scrape.")
def scrape(once: bool, market: str):
    """Scrape news and social media, analyze sentiment, and update signals."""
    console.print(f"[bold cyan]Starting scraper (market={market})...[/bold cyan]")
    components = _build_components()
    runner = components["runner"]

    if once:
        runner.run_once(market=market)
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

        # Pre-warm the Screener's per-market cache for both HK + US in
        # background threads. The first user to land on the Screener tab
        # then hits warm cache (~0ms data fetch) instead of paying the
        # 2.5s cold Supabase round-trip. Threads daemon so dashboard
        # boot isn't blocked if a warm-up errors.
        #
        # `SKIP_DASHBOARD_PREWARM=true` is the escape hatch used by the CI
        # smoke job — CI runs against an empty SQLite DB with no Supabase
        # creds, so the warm-up has nothing useful to do and the failed
        # network calls just add noise to the log.
        if os.getenv("SKIP_DASHBOARD_PREWARM", "").lower() != "true":
            import threading
            from dashboard.screener_callbacks import _query_latest
            def _warm(market):
                try:
                    t0 = time.time()
                    rows = _query_latest(components["settings"].DB_PATH, market=market)
                    console.print(f"[dim]Screener cache warmed: {market} · "
                                  f"{len(rows):,} rows · {time.time()-t0:.1f}s[/dim]")
                except Exception as e:
                    console.print(f"[yellow]Pre-warm {market} failed: {e}[/yellow]")
            threading.Thread(target=_warm, args=("HK",), daemon=True).start()
            threading.Thread(target=_warm, args=("US",), daemon=True).start()

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

    # Sync the reconciler-resolved bilingual names + sector taxonomy up to
    # Supabase and back into the local mirror — same hook as universe-us seed.
    try:
        from analysis.data_loader import (
            push_securities_reference, refresh_securities_reference_cache,
        )
        from storage.database import Database
        db_local = Database(settings.DB_PATH)
        push = push_securities_reference(db_local)
        pull = refresh_securities_reference_cache(db_local)
        console.print(f"[dim]securities_reference: pushed "
                       f"{push['pushed']:,} → Supabase, "
                       f"mirrored {pull['written']:,} → local "
                       f"(total {push['elapsed_s']+pull['elapsed_s']:.1f}s)[/dim]")
    except Exception as e:
        console.print(f"[yellow]securities_reference sync skipped: {e}[/yellow]")


@cli.group("universe-us")
def universe_us():
    """Manage the US-listed securities universe (Russell 3000)."""


@universe_us.command("seed")
@click.option("--source",
                type=click.Choice(["nasdaqtrader", "ishares", "wikipedia"]),
                default="nasdaqtrader", show_default=True,
                help="nasdaqtrader = NASDAQ+NYSE common stock (~7,000 names, "
                     "recommended) | ishares = Russell 3000 (~3,000, requires "
                     "session cookie — often blocked) | wikipedia = "
                     "S&P 500 + Nasdaq-100 + Dow 30 union (~600).")
def universe_us_seed(source: str):
    """Download the US universe list and reconcile into the `securities` table."""
    import config.settings as settings
    from storage.database import Database
    from storage.repository import SecuritiesRepository
    from universe import us_loader, reconciler

    console.print(f"[bold cyan]Seeding US universe (source={source})...[/bold cyan]")
    db = Database(settings.DB_PATH)
    db.initialize()
    securities_repo = SecuritiesRepository(db)

    records = us_loader.download_and_parse(settings.HKEX_CACHE_DIR, source=source)
    if not records:
        console.print("[red]No US records obtained from any source. Aborting.[/red]")
        return

    # Load the curated US watchlist YAML (~40 mega-cap names) so the
    # reconciler flags them as is_watchlist=1 + applies aliases for the
    # ticker matcher. The file may be empty/missing in early bootstraps —
    # load_watchlist() returns {"sectors": {}} as a graceful fallback.
    watchlist_us = settings.load_watchlist(market="US")
    summary = reconciler.reconcile_us(securities_repo, records, watchlist_us=watchlist_us)

    table = Table(title="US Universe Reconcile Summary", show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("US securities (active)", str(summary["total_us"]))
    table.add_row("US rows ingested", str(summary["us_ingested"]))
    table.add_row("US watchlist tickers", str(summary["watchlist_us"]))
    table.add_row("US tickers missing from universe",
                    str(len(summary["missing_from_universe"])))
    table.add_row("US deactivated (delisted)", str(summary.get("deactivated", 0)))
    table.add_row("Sub-sector assignments",
                    str(summary.get("sub_sector_assigned", 0)))
    console.print(table)
    if summary["missing_from_universe"]:
        console.print(f"[yellow]Missing from universe (kept as overrides): "
                        f"{summary['missing_from_universe']}[/yellow]")

    # Sync the reconciler-resolved bilingual names + sector taxonomy up to
    # Supabase so the cloud `securities_reference` table stays the source
    # of truth. Then pull back into the local SQLite mirror so the dashboard
    # reads sub-millisecond. Non-fatal if cloud is unavailable.
    try:
        from analysis.data_loader import (
            push_securities_reference, refresh_securities_reference_cache,
        )
        from storage.database import Database
        db = Database(settings.DB_PATH)
        push = push_securities_reference(db)
        pull = refresh_securities_reference_cache(db)
        console.print(f"[dim]securities_reference: pushed "
                       f"{push['pushed']:,} → Supabase, "
                       f"mirrored {pull['written']:,} → local "
                       f"(total {push['elapsed_s']+pull['elapsed_s']:.1f}s)[/dim]")
    except Exception as e:
        console.print(f"[yellow]securities_reference sync skipped: {e}[/yellow]")


@universe_us.command("refresh")
@click.option("--source", type=click.Choice(["ishares", "wikipedia"]),
                default="ishares", show_default=True)
def universe_us_refresh(source: str):
    """Alias for `seed` — same idempotent flow, intended for scheduled runs."""
    ctx = click.get_current_context()
    ctx.invoke(universe_us_seed, source=source)


@universe_us.command("refresh-sectors")
@click.option("--throttle", default=0.5, show_default=True, type=float,
              help="Seconds between yfinance .info calls.")
@click.option("--force-all", is_flag=True, default=False,
              help="Re-fetch every active US ticker. Default skips rows "
                   "that already have yf_sector set (incremental backfill).")
def universe_us_refresh_sectors(throttle: float, force_all: bool):
    """Tier 4: Backfill `yf_sector` + `yf_industry` for US tickers via
    yfinance `.info`. Sector + industry only (no financial ratios). After
    completion, re-run `python main.py universe-us seed` so the reconciler's
    industry-to-subsector map derives `sub_sector` for the newly-tagged rows.
    """
    import config.settings as settings
    from storage.database import Database
    from storage.repository import SecuritiesRepository
    from scrapers.us_sector_scraper import fetch_many

    db = Database(settings.DB_PATH)
    db.initialize()
    securities_repo = SecuritiesRepository(db)

    rows = securities_repo.get_all_active(market="US")
    if force_all:
        targets = [r["ticker"] for r in rows]
    else:
        targets = [r["ticker"] for r in rows if not r.get("yf_sector")]
    console.print(f"[bold cyan]Sector backfill: {len(targets):,} of "
                  f"{len(rows):,} US tickers "
                  f"({'force-all' if force_all else 'skip already-tagged'})[/bold cyan]")
    if not targets:
        console.print("[green]All US tickers already tagged. Nothing to do.[/green]")
        return

    summary = fetch_many(targets, securities_repo, throttle_seconds=throttle)
    console.print(f"\n[bold green]Complete:[/bold green] attempted={summary['attempted']:,} "
                  f"tagged={summary['tagged']:,} "
                  f"both_null={summary['both_null']:,} "
                  f"errors={summary['errors']:,}")
    console.print("\n[dim]Now re-run `python main.py universe-us seed` to derive "
                  "sub_sector for the newly-tagged rows.[/dim]")


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
def names():
    """Bilingual display-name lookup table (`securities_meta`) — populates
    Chinese names per ticker so the dashboard can flip EN/中文 names instantly."""


@names.command("refresh")
@click.option("--market", default="ALL", show_default=True,
              type=click.Choice(["HK", "US", "ALL"]),
              help="Which market's tickers to seed Chinese names for.")
@click.option("--throttle", default=0.3, show_default=True, type=float,
              help="Seconds between akshare calls.")
@click.option("--force/--skip-seeded", default=False,
              help="--force re-fetches every ticker; default skips rows that "
                   "already have both names.")
def names_refresh(market: str, throttle: float, force: bool):
    """Populate `securities_meta.chinese_name` for the given market(s) by
    extracting `SECURITY_NAME_ABBR` from akshare's per-ticker fundamentals
    response (the same endpoint used by `historical seed`). English names
    come from `securities.name`.
    """
    import config.settings as settings
    from storage.database import Database
    from storage.repository import SecuritiesRepository, StockNamesRepository
    from scrapers.stock_names_scraper import seed_names_for_market

    db = Database(settings.DB_PATH)
    db.initialize()
    securities_repo = SecuritiesRepository(db)
    names_repo = StockNamesRepository(db)

    markets = ["HK", "US"] if market == "ALL" else [market]
    for m in markets:
        console.print(f"\n[bold cyan]Seeding stock names for {m} "
                      f"({'force' if force else 'skip-seeded'})...[/bold cyan]")
        summary = seed_names_for_market(
            securities_repo, names_repo, m,
            throttle_seconds=throttle,
            skip_already_seeded=not force,
        )
        console.print(f"  attempted: {summary['attempted']}, "
                      f"with_chinese: {summary['with_chinese']}, "
                      f"without_chinese: {summary['without_chinese']}, "
                      f"failed: {summary['failed']}, "
                      f"skipped_already_seeded: {summary['skipped_already_seeded']}")

    total = names_repo.count()
    with_zh = names_repo.count_with_chinese()
    console.print(f"\n[bold green]securities_meta total: {total:,} rows · "
                  f"with Chinese: {with_zh:,} ({100*with_zh/total if total else 0:.1f}%)[/bold green]")


@cli.group()
def historical():
    """Historical fundamentals (akshare) + multi-year price data (yfinance) for backtesting."""


@historical.command("refresh-latest-prices")
def historical_refresh_latest_prices():
    """Refresh the local `latest_prices` SQLite cache from Supabase
    `historical_prices`. Powers the Screener's mcap/P/E enrichment without
    paying the ~40s DISTINCT-ON cost per render. Safe to run at any time;
    also invoked nightly by the EOD price cron."""
    import config.settings as settings
    from storage.database import Database
    from analysis.data_loader import refresh_latest_prices_cache

    db = Database(settings.DB_PATH)
    db.initialize()
    console.print("[bold cyan]Refreshing latest_prices cache...[/bold cyan]")
    summary = refresh_latest_prices_cache(db)
    console.print(f"  requested: {summary['requested']:,}")
    console.print(f"  fetched:   {summary['fetched']:,}")
    console.print(f"  written:   {summary['written']:,}")
    console.print(f"  elapsed:   {summary['elapsed_s']:.1f}s")


@historical.command("seed")
@click.option("--tickers", default="WATCHLIST", show_default=True,
              help="ALL | WATCHLIST | comma-separated tickers")
@click.option("--market", default="HK", show_default=True,
              type=click.Choice(["HK", "US", "ALL"]),
              help="Restrict ALL/WATCHLIST targets to this market. "
                   "Ignored when --tickers is a comma-separated list.")
@click.option("--throttle", default=0.5, show_default=True, type=float,
              help="Seconds between akshare requests.")
@click.option("--skip-prices", is_flag=True, default=False,
              help="Skip the yfinance price-history pull (only seed fundamentals).")
@click.option("--skip-fundamentals", is_flag=True, default=False,
              help="Skip the akshare fundamentals pull (only seed prices).")
@click.option("--price-period", default="10y", show_default=True,
              help="Period for yfinance history (e.g. 5y, 10y, max).")
def historical_seed(tickers, market, throttle, skip_prices, skip_fundamentals,
                     price_period):
    """One-time backfill: pull akshare historical fundamentals (HK only) +
    yfinance multi-year prices (both markets). akshare is HK-only — for US
    targets, Stage A is automatically skipped."""
    import config.settings as settings
    from storage.database import Database
    from storage.repository import SecuritiesRepository
    from scrapers.akshare_historical_scraper import fetch_many as ak_fetch_many
    from scrapers.historical_price_scraper import fetch_many as price_fetch_many
    from storage.factory import get_prices_repo, get_fundamentals_repo

    db = Database(settings.DB_PATH)
    db.initialize()
    securities_repo = SecuritiesRepository(db)
    # Both repos route through the factory so writes go to Supabase under
    # USE_CLOUD_DB=true (matches the cache-aside layer used by the
    # dashboard's Research / Discovery callbacks).
    fundamentals_repo = get_fundamentals_repo(db)
    prices_repo = get_prices_repo(db)

    selector = tickers.strip().upper()

    def _market_filter(rows):
        if market == "ALL":
            return rows
        return [r for r in rows if (r.get("market") or "HK") == market]

    if selector == "ALL":
        target = [s["ticker"] for s in _market_filter(securities_repo.get_universe())]
        console.print(f"[bold cyan]Seeding historical data for {len(target)} active securities "
                      f"(market={market})...[/bold cyan]")
        console.print("[yellow]Fundamentals: ~6-8 hours at 0.5s throttle. Prices: ~10-15 min in batches.[/yellow]")
    elif selector == "WATCHLIST":
        target = [s["ticker"] for s in _market_filter(securities_repo.get_watchlist())]
        console.print(f"[bold cyan]Seeding historical data for {len(target)} watchlist tickers "
                      f"(market={market})...[/bold cyan]")
    else:
        target = [t.strip() for t in tickers.split(",") if t.strip()]
        console.print(f"[bold cyan]Seeding historical data for {len(target)} ticker(s): {target}[/bold cyan]")

    if not target:
        console.print("[red]No tickers. Did you run the right `universe-*` seed first?[/red]")
        return

    # Stage A: historical annual fundamentals. The HK path goes through
    # akshare directly (~9 years per ticker). The US path goes through a
    # hybrid akshare-primary + yfinance-fallback scraper because akshare's
    # US endpoint misses the entire Financials sector. For market='ALL'
    # we run both paths in sequence.
    if not skip_fundamentals:
        hk_targets = [t for t in target
                       if (t.endswith(".HK") or t.startswith("^HSI")
                            or t.startswith("^HSCEI") or t.startswith("^HSTECH"))]
        us_targets = [t for t in target if t not in set(hk_targets)
                       and not t.startswith(("&", "@", "^"))]

        if market in ("HK", "ALL") and hk_targets:
            console.print(f"\n[bold]Stage A1: akshare historical fundamentals "
                            f"(HK, annual, ~9 years) — {len(hk_targets)} tickers[/bold]")
            ak_summary = ak_fetch_many(hk_targets, fundamentals_repo,
                                        securities_repo, throttle_seconds=throttle)
            console.print(f"  attempted: {ak_summary['attempted']}, "
                          f"snapshots_written: {ak_summary['snapshots_written']}, "
                          f"no_data: {ak_summary['no_data_tickers']}, "
                          f"failed: {ak_summary['failed_tickers']}")

        if market in ("US", "ALL") and us_targets:
            from scrapers.us_fundamentals_scraper import fetch_many as us_fetch_many
            console.print(f"\n[bold]Stage A2: US historical fundamentals "
                            f"(akshare → yfinance fallback) — {len(us_targets)} tickers[/bold]")
            us_summary = us_fetch_many(us_targets, fundamentals_repo,
                                         throttle_seconds=throttle)
            console.print(f"  attempted: {us_summary['attempted']}, "
                          f"snapshots_written: {us_summary['snapshots_written']}, "
                          f"akshare_hit: {us_summary['akshare_hit']}, "
                          f"yfinance_fallback: {us_summary['yfinance_fallback']}, "
                          f"no_data: {us_summary['no_data_tickers']}, "
                          f"failed: {us_summary['failed_tickers']}")

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


@backtest.command("factor-verify")
@click.option("--market", default="HK", show_default=True,
                type=click.Choice(["HK", "US"]),
                help="Market scope for the V/Q/G universe.")
@click.option("--start", default="2023-01-01", show_default=True,
                help="Start date (YYYY-MM-DD).")
@click.option("--end", default=None, help="End date. Defaults to today.")
@click.option("--freq", default="1m", show_default=True,
                type=click.Choice(["1d", "3d", "1w", "1m"]),
                help="Rebalance stride in trading days.")
@click.option("--min-names", default=10, show_default=True, type=int,
                help="Minimum ranked tickers per sub-sector to include "
                       "in the decile cuts.")
@click.option("--out-csv", default=None,
                help="Optional CSV output of (decile, mean_return, n_obs) + "
                       "the IC time series.")
def backtest_factor_verify(market, start, end, freq, min_names, out_csv):
    """Long top decile / short bottom decile V/Q/G factor-efficacy test.

    Sub-sector-neutral · equal-weight within each leg · dollar-neutral
    long+short. Outputs annualised long/short/spread returns, the Spread
    Sharpe at rf=3%, mean Information Coefficient + its t-stat, and the
    per-decile mean forward-return ladder (the monotonicity test).

    Paper signal test only — no transaction costs, no borrow fees, no
    HK shorting constraints. Cf. analysis/factor_backtest.run_factor_
    verification_backtest for the algorithm."""
    from datetime import date as date_cls
    import config.settings as settings
    from analysis.factor_backtest import run_factor_verification_backtest

    end = end or date_cls.today().strftime("%Y-%m-%d")
    console.print(f"[bold cyan]Factor verification: {market} | freq={freq} | "
                  f"{start} to {end}[/bold cyan]")

    result = run_factor_verification_backtest(
        start_date=start, end_date=end, rebalance_freq=freq,
        db_path=settings.DB_PATH, market=market,
        min_names_per_subsector=min_names,
    )
    m = result.metrics

    def pct(v):
        return f"{v*100:+.2f}%" if v is not None else "N/A"
    def num(v, d=3):
        return f"{v:+.{d}f}" if v is not None else "N/A"

    table = Table(title=f"Long/Short V/Q/G verification — {market}",
                    show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Window actually traded",
                    f"{result.actual_start} → {result.actual_end}")
    table.add_row("Sub-sectors used (≥10 names)",
                    f"{result.n_subsectors_used}")
    table.add_row("Avg long basket size", f"{result.avg_long_size:.0f}")
    table.add_row("Avg short basket size", f"{result.avg_short_size:.0f}")
    table.add_row("N rebalance periods", f"{m['n_periods']}")
    table.add_row("", "")
    table.add_row("Long leg ann. return", pct(m["ann_return_long"]))
    table.add_row("Short leg ann. return", pct(m["ann_return_short"]))
    table.add_row("[bold]Spread ann. return[/bold]",
                    f"[bold]{pct(m['ann_return_spread'])}[/bold]")
    table.add_row("Spread ann. vol", pct(m["ann_vol_spread"]))
    table.add_row("Spread Sharpe (rf=3%)", num(m["spread_sharpe"]))
    table.add_row("Spread max drawdown", pct(m["spread_max_dd"]))
    table.add_row("Hit rate (long > short)", pct(m["hit_rate"]))
    table.add_row("Mean Information Coefficient",
                    num(m["mean_ic"], 4))
    table.add_row("IC t-stat", num(m["ic_tstat"], 2))
    console.print(table)

    # Decile monotonicity ladder
    dec_table = Table(title="Decile forward-return ladder "
                            "(1 = bottom, 10 = top)",
                       show_lines=False)
    dec_table.add_column("Decile", style="bold")
    dec_table.add_column("Mean period return", justify="right")
    dec_table.add_column("Observations", justify="right")
    for b in range(1, 11):
        dec_table.add_row(f"D{b}", pct(result.decile_returns.get(b, 0.0)),
                           str(result.decile_counts.get(b, 0)))
    console.print(dec_table)

    # Pass/fail criteria summary
    spread_real = (m["ann_return_spread"] > 0
                     and m["spread_sharpe"] > 0.5
                     and m["mean_ic"] > 0.02)
    d10 = result.decile_returns.get(10, 0.0)
    d1 = result.decile_returns.get(1, 0.0)
    monotone_endpoints = d10 > d1
    n_correct_steps = sum(1 for b in range(1, 10)
                            if result.decile_returns.get(b + 1, 0.0)
                                 > result.decile_returns.get(b, 0.0))
    console.print()
    console.print("[bold]Factor verdict:[/bold]")
    console.print(f"  Spread real (ann>0, Sharpe>0.5, IC>0.02): "
                  f"{'[green]YES[/green]' if spread_real else '[yellow]NO[/yellow]'}")
    console.print(f"  D10 > D1 (endpoint monotonicity): "
                  f"{'[green]YES[/green]' if monotone_endpoints else '[red]NO[/red]'}"
                  f" (D10={pct(d10)}, D1={pct(d1)})")
    console.print(f"  Inner-step monotonicity: "
                  f"{n_correct_steps}/9 steps go in the right direction")

    if out_csv:
        import csv
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["section", "key", "value"])
            for k, v in m.items():
                w.writerow(["metrics", k, v])
            for b in range(1, 11):
                w.writerow(["decile",
                              f"D{b}",
                              result.decile_returns.get(b, 0.0)])
                w.writerow(["decile_n",
                              f"D{b}",
                              result.decile_counts.get(b, 0)])
            for d, ic in result.ic_series:
                w.writerow(["ic", d, ic])
        console.print(f"  CSV: [cyan]{out_csv}[/cyan]")


def _print_sector_signals(sector_signal_repo):
    signals = sector_signal_repo.get_latest_signals()
    if not signals:
        console.print("[yellow]No sector signals yet. Try running after scraping.[/yellow]")
        return

    table = Table(title="Croissant Stock Analyser — Sector Signals", show_lines=True)
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
