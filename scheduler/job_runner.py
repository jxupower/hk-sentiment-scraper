from datetime import date, datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from utils.logger import get_logger

logger = get_logger(__name__)


class JobRunner:
    def __init__(self, config, scrapers, analyzer, signal_gen, sector_signal_gen,
                 article_repo, sentiment_repo, signal_repo, sector_signal_repo,
                 yahoo_scraper, search_terms, all_tickers, watchlist,
                 securities_repo=None, fundamentals_repo=None,
                 interval_minutes=30):
        self._config = config
        self._scrapers = scrapers
        self._analyzer = analyzer
        self._signal_gen = signal_gen
        self._sector_signal_gen = sector_signal_gen
        self._article_repo = article_repo
        self._sentiment_repo = sentiment_repo
        self._signal_repo = signal_repo
        self._sector_signal_repo = sector_signal_repo
        self._securities_repo = securities_repo
        self._fundamentals_repo = fundamentals_repo
        self._yahoo = yahoo_scraper
        self._search_terms = search_terms
        self._all_tickers = all_tickers
        self._watchlist = watchlist
        self._interval = interval_minutes
        self._scheduler = BackgroundScheduler(timezone="UTC")

    def start(self):
        self._scheduler.add_job(
            func=self._scrape_and_analyze,
            trigger="interval",
            minutes=self._interval,
            id="main_scrape",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.utcnow(),
        )
        self._scheduler.add_job(
            func=self._prune_old_data,
            trigger="cron",
            hour=3,
            id="db_prune",
        )
        if self._securities_repo is not None:
            self._scheduler.add_job(
                func=self._refresh_universe,
                trigger="cron",
                day_of_week="sun",
                hour=2,
                id="universe_refresh",
            )
        # NOTE: Daily yfinance fundamentals snapshot cron is DROPPED in the
        # cloud-DB migration. Reason: would write ~2,769 rows/day to Supabase,
        # ballooning the free tier (500MB cap) within months. The Research tab
        # now fetches current ratios on demand via
        # analysis.data_loader.get_or_fetch_latest_fundamentals(), and the
        # akshare annual history covers multi-year analysis. Re-enable if you
        # truly need a daily snapshot history beyond what's cached on demand.
        # Backtest infrastructure refresh — only registered if securities_repo wired
        if self._securities_repo is not None:
            # Weekly full-window catch-up (Sundays 04:00 UTC, period=1y) — heals
            # any tickers that the daily run failed on across the week.
            self._scheduler.add_job(
                func=self._refresh_historical_prices,
                trigger="cron",
                day_of_week="sun",
                hour=4,
                id="historical_prices_refresh",
                max_instances=1,
            )
            # Daily EOD price refresh — weekdays 14:00 UTC (22:00 HKT), 6 hours
            # after HK market close so yfinance has settled the day's bar.
            # Short 5-day window keeps the run under ~10 minutes.
            # misfire_grace_time=3600 means a missed firing within an hour
            # (e.g. dashboard restarted at 14:30 UTC) still executes.
            self._scheduler.add_job(
                func=self._refresh_historical_prices_daily,
                trigger="cron",
                day_of_week="mon-fri",
                hour=14,
                id="historical_prices_daily",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            # Sub-sector composites rebuild — weekdays 14:30 UTC (22:30 HKT),
            # 30 min after the EOD price refresh so constituents have
            # already settled. Rebuilds all ~75 `&NAME` indices into
            # historical_prices.
            self._scheduler.add_job(
                func=self._refresh_subsector_composites_daily,
                trigger="cron",
                day_of_week="mon-fri",
                hour=14,
                minute=30,
                id="subsector_composites_daily",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            # Startup freshness check — fires once ~30s after dashboard start.
            # If historical_prices is more than 1 calendar day stale (e.g.
            # because the cron didn't fire when the dashboard was offline),
            # triggers an immediate refresh. Keeps the user from having to
            # wait until the next 14:00 UTC cron firing for fresh data.
            self._scheduler.add_job(
                func=self._refresh_prices_if_stale,
                trigger="date",
                run_date=datetime.utcnow() + timedelta(seconds=30),
                id="historical_prices_startup_check",
                max_instances=1,
            )
            self._scheduler.add_job(
                func=self._reoptimize_parameters,
                trigger="cron",
                day="1",         # first day of each month
                hour=5,
                id="reoptimize_parameters",
                max_instances=1,
            )
        self._scheduler.start()
        logger.info("Scheduler started. Scraping every %d minutes.", self._interval)

    def stop(self):
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")

    def run_once(self, market: str = "HK"):
        """Run a single scrape cycle. `market` defaults to HK to preserve
        existing behaviour; pass 'US' to scrape US sentiment, or 'ALL' to
        scrape both sequentially (HK first, then US)."""
        market = (market or "HK").upper()
        if market == "ALL":
            self._scrape_and_analyze("HK")
            self._scrape_and_analyze("US")
        else:
            self._scrape_and_analyze(market)

    def _build_dynamic_terms(self, market: str = "HK"):
        """Pull fresh search terms from the securities table for one market.

        Returns (full_terms, watchlist_terms, watchlist_tickers).
        Falls back to the static YAML-derived terms if no securities_repo is wired.
        Filters securities rows by market so a US scrape doesn't pull HK names.
        """
        market = (market or "HK").upper()
        if self._securities_repo is None:
            return self._search_terms, self._search_terms, set(self._search_terms.keys())
        rows = [r for r in self._securities_repo.get_all_active()
                 if (r.get("market") or "HK") == market]
        full_terms = self._config.build_search_terms_from_db(
            rows, watchlist_only=False, market=market)
        watchlist_terms = self._config.build_search_terms_from_db(
            rows, watchlist_only=True, market=market)
        return full_terms, watchlist_terms, set(watchlist_terms.keys())

    def _scrape_and_analyze(self, market: str = "HK"):
        from scrapers.rss_scraper import RssScraper

        market = (market or "HK").upper()
        logger.info("=== Scrape cycle started at %s (market=%s) ===",
                     datetime.utcnow().isoformat(), market)
        full_terms, watchlist_terms, watchlist_tickers = self._build_dynamic_terms(market)
        logger.info("Search terms: %d total, %d watchlist (market=%s)",
                     len(full_terms), len(watchlist_terms), market)

        # Swap in the right RSS feeds for this market. The existing RssScraper
        # instance was built with HK feeds at startup — for US we rebuild
        # in-place by overwriting `feed_configs`. Yahoo + Reddit scrapers are
        # per-ticker so they don't need market-specific feed lists; the
        # watchlist_terms filter already restricts them to the right universe.
        for scraper in self._scrapers:
            if isinstance(scraper, RssScraper):
                scraper.feed_configs = self._config.load_rss_feeds(market=market)

        all_articles = []
        for scraper in self._scrapers:
            if not scraper.is_available():
                continue
            try:
                scraper_name = type(scraper).__name__
                if scraper_name in ("YahooScraper", "RedditScraper"):
                    terms = watchlist_terms  # per-ticker scrapers don't scale to universe
                else:
                    terms = full_terms
                if isinstance(scraper, RssScraper):
                    scraper.watchlist_tickers = watchlist_tickers  # for matcher tie-breaking
                articles = scraper.fetch(terms)
                all_articles.extend(articles)
            except Exception as e:
                logger.error("Scraper %s failed: %s", type(scraper).__name__, e)

        new_count = 0
        for article in all_articles:
            if self._article_repo.article_exists(article.url):
                continue
            article_id = self._article_repo.insert_article(
                source=article.source,
                title=article.title,
                body=article.body,
                url=article.url,
                published_at=article.published_at,
                author=article.author,
                raw_score=article.raw_score,
                tickers=article.ticker_hints,
                market=market,
            )
            if article_id is None:
                continue
            new_count += 1
            result = self._analyzer.score_article(
                title=article.title,
                body=article.body,
                url=article.url,
                ticker_hints=article.ticker_hints,
            )
            for ticker in article.ticker_hints:
                self._sentiment_repo.insert_score(
                    article_id=article_id,
                    ticker=ticker,
                    vader_score=result.vader_score,
                    claude_score=result.claude_score,
                    final_score=result.final_score,
                    label=result.label,
                )

        logger.info("Stored %d new articles.", new_count)

        # ==============================================================
        # Universe-wide sub-sector roll-up (post 2026-06 redesign).
        #
        # Replaces the previous watchlist-bounded iteration (~50 tickers
        # per market) with a roll-up over every active sub-sector-tagged
        # ticker in the market (~2.7k HK + ~3.9k US). Coverage near 100%
        # for any sub-sector that exists in the universe.
        #
        # Compute cost stays bounded by switching the price-momentum
        # source from per-ticker `yahoo.fetch_price_history(period='1mo')`
        # (~50s for 50 tickers, would be ~115 min for 6.7k) to one
        # `bulk_get_price_series(...)` Supabase call per market (~30-60s
        # for the whole batch).
        # ==============================================================
        from collections import defaultdict
        from storage.repository import SecuritiesReferenceRepository
        from storage.factory import get_prices_repo
        from analysis.sentiment_aggregation import (
            compute_ticker_momentum, aggregate_subsector_signals,
        )

        ref_repo = SecuritiesReferenceRepository(self._securities_repo.db)
        prices_repo = get_prices_repo(self._securities_repo.db)
        universe_rows = ref_repo.get_market_universe(market)
        market_tickers = [r["ticker"] for r in universe_rows]
        ticker_subsector = {r["ticker"]: r["sub_sector"] for r in universe_rows}

        subsector_to_tickers: dict[str, list[str]] = defaultdict(list)
        for t, sub in ticker_subsector.items():
            subsector_to_tickers[sub].append(t)
        logger.info(
            "Universe roll-up [%s]: %d tickers across %d sub-sectors",
            market, len(market_tickers), len(subsector_to_tickers),
        )

        # -- One bulk price pull covers every ticker's 5-day momentum. --
        momentum_by_ticker = compute_ticker_momentum(
            prices_repo, market_tickers, lookback_days=5, window_days=35)

        # -- Bulk read sentiment + article counts from the local DB. --
        sentiment_by_ticker: dict[str, float | None] = {}
        article_count_by_ticker: dict[str, int] = {}
        for ticker in market_tickers:
            scores_24h = self._sentiment_repo.get_scores_for_ticker(ticker, hours=24)
            if scores_24h:
                vals = [s.get("final_score") for s in scores_24h
                          if s.get("final_score") is not None]
                sentiment_by_ticker[ticker] = (
                    sum(vals) / len(vals) if vals else None)
                article_count_by_ticker[ticker] = len(scores_24h)
            else:
                sentiment_by_ticker[ticker] = None
                article_count_by_ticker[ticker] = 0

        # -- Mcap lookup (used for weighting sub-sector aggregates). --
        # Pulled from the latest_prices cache + shares_outstanding from
        # fundamentals_snapshots when available; degrades to equal-weight
        # on missing values.
        mcap_by_ticker: dict[str, float | None] = {}
        try:
            from analysis.data_loader import get_universe_fundamentals
            from storage.database import Database
            fund_rows = get_universe_fundamentals(
                Database(self._securities_repo.db.db_path), market=market)
            for f in fund_rows:
                mcap = f.get("market_cap")
                if mcap is None:
                    last_px = f.get("last_price")
                    shares = f.get("shares_outstanding")
                    if last_px and shares:
                        try:
                            mcap = float(last_px) * float(shares)
                        except (TypeError, ValueError):
                            mcap = None
                mcap_by_ticker[f["ticker"]] = mcap
        except Exception as e:
            logger.warning("mcap lookup failed for %s — using equal weights: %s",
                            market, e)

        # -- Per-ticker signal upserts (drill-down). --
        # Replaces the per-ticker price-history yfinance fetch with the
        # already-computed bulk momentum. Cheap loop now.
        for ticker in market_tickers:
            try:
                sent_24h = sentiment_by_ticker.get(ticker)
                mom_5d = momentum_by_ticker.get(ticker)
                n_articles = article_count_by_ticker.get(ticker, 0)

                # BUY/SELL/HOLD/WATCH decision via the existing thresholds
                # (analysis/signals.py:compute_ticker_signal). We avoid
                # calling the helper because it expects a pandas price_df;
                # the rules below mirror the same logic.
                signal = "HOLD"
                if sent_24h is not None and mom_5d is not None:
                    if sent_24h > 0.2 and mom_5d > 0:
                        signal = "BUY"
                    elif sent_24h < -0.2 and mom_5d < 0:
                        signal = "SELL"
                    elif (sent_24h > 0.2 and mom_5d < 0) or (
                          sent_24h < -0.2 and mom_5d > 0):
                        signal = "WATCH"

                confidence = 0.0
                if sent_24h is not None:
                    mag = min(abs(sent_24h) / 0.3, 1.0)
                    vol = min(n_articles / 20.0, 1.0)
                    confidence = round((mag + vol) / 2.0, 2)

                self._signal_repo.upsert_signal(
                    ticker=ticker,
                    sector=ticker_subsector.get(ticker) or "Unclassified",
                    avg_sentiment_24h=round(sent_24h, 4) if sent_24h is not None else None,
                    avg_sentiment_7d=None,
                    article_count_24h=n_articles,
                    price_momentum_5d=round(mom_5d, 4) if mom_5d is not None else None,
                    signal=signal, confidence=confidence,
                )
            except Exception as e:
                logger.error("Ticker signal failed [%s]: %s", ticker, e)

        # -- Sub-sector roll-up (mcap-weighted). --
        sector_rows = aggregate_subsector_signals(
            subsector_to_tickers=subsector_to_tickers,
            sentiment_by_ticker=sentiment_by_ticker,
            article_count_by_ticker=article_count_by_ticker,
            momentum_by_ticker=momentum_by_ticker,
            mcap_by_ticker=mcap_by_ticker,
        )
        for row in sector_rows:
            try:
                self._sector_signal_repo.insert_signal(
                    sector=row["sector"],
                    avg_sentiment_24h=row["avg_sentiment_24h"],
                    avg_sentiment_7d=row["avg_sentiment_7d"],
                    article_count_24h=row["article_count_24h"],
                    avg_price_momentum=row["avg_price_momentum"],
                    direction=row["direction"],
                    confidence=row["confidence"],
                )
                logger.info(
                    "Sub-sector [%s]: %s (sentiment=%s, momentum=%s, articles=%d, confidence=%.2f)",
                    row["sector"], row["direction"],
                    f"{row['avg_sentiment_24h']:.3f}" if row['avg_sentiment_24h'] is not None else "—",
                    f"{row['avg_price_momentum']:.2f}%" if row['avg_price_momentum'] is not None else "—",
                    row["article_count_24h"], row["confidence"],
                )
            except Exception as e:
                logger.error("Sub-sector insert failed [%s]: %s",
                              row["sector"], e)

        logger.info("=== Scrape cycle complete ===")

    def _prune_old_data(self):
        self._article_repo.prune_old_articles(days=90)

    def _refresh_universe(self):
        from universe import hkex_loader, reconciler
        try:
            records = hkex_loader.download_and_parse(self._config.HKEX_CACHE_DIR)
            reconciler.reconcile(self._securities_repo, records, self._watchlist)
        except Exception as e:
            logger.error("Weekly universe refresh failed: %s", e)

    def _refresh_historical_prices(self):
        """Weekly yfinance multi-year price history refresh — keeps the backtest
        engine's data fresh as new trading days land. Routes writes through the
        factory so cloud DB receives the updates when USE_CLOUD_DB=true."""
        from storage.factory import get_prices_repo
        from scrapers.historical_price_scraper import fetch_many as price_fetch_many
        try:
            tickers = [s["ticker"] for s in self._securities_repo.get_universe()]
            prices_repo = get_prices_repo(self._securities_repo.db)
            # Only need the last year of new data; UPSERT handles dup dates.
            price_fetch_many(tickers, prices_repo, period="1y")
        except Exception as e:
            logger.error("Weekly historical price refresh failed: %s", e)

    def _refresh_historical_prices_daily(self):
        """Daily EOD yfinance price refresh — weekdays at 22:00 HKT (14:00 UTC).
        Short 5-day window keeps the run to ~5-10 min on ~2,778 tickers; UPSERT
        no-ops on dates already present. Same fetch_many path as the weekly
        Sunday job, just a tighter window for everyday top-up.

        After the bulk fetch completes, also refreshes the local
        `latest_prices` SQLite cache so the Screener's mcap/P/E enrichment
        stays sub-second on the next page render — without this the cold
        load would re-incur the ~40s DISTINCT-ON-by-ticker over Supabase
        `historical_prices`."""
        from storage.factory import get_prices_repo
        from scrapers.historical_price_scraper import fetch_many as price_fetch_many
        from analysis.data_loader import refresh_latest_prices_cache
        try:
            tickers = [s["ticker"] for s in self._securities_repo.get_universe()]
            prices_repo = get_prices_repo(self._securities_repo.db)
            summary = price_fetch_many(tickers, prices_repo, period="5d")
            logger.info("Daily price refresh: %s", summary)
        except Exception as e:
            logger.error("Daily historical price refresh failed: %s", e)
        # Refresh the latest_prices SQLite cache regardless of whether the
        # bulk fetch above succeeded — even a stale cache is faster than
        # no cache, and the cache will catch up next time the EOD fetch lands.
        try:
            cache_summary = refresh_latest_prices_cache(
                self._securities_repo.db)
            logger.info("latest_prices cache refresh: %s", cache_summary)
        except Exception as e:
            logger.error("latest_prices cache refresh failed: %s", e)

    def _refresh_subsector_composites_daily(self):
        """Daily rebuild of all `&NAME` sub-sector composite indices.
        Runs 30 min after the EOD price refresh so the constituents have
        already settled. Per-composite errors are caught + logged
        individually — one bad sub-sector doesn't stop the others."""
        from analysis.subsector_synth import rebuild_all_subsectors
        try:
            summary = rebuild_all_subsectors(self._securities_repo.db)
            logger.info(
                "Daily sub-sector composite refresh: %d/%d succeeded, "
                "%d rows in %.1fs",
                summary["n_succeeded"], summary["n_attempted"],
                summary["total_rows_written"], summary["elapsed_sec"])
            if summary.get("errors"):
                logger.warning("Composite rebuild errors: %s",
                                 summary["errors"][:5])
        except Exception as e:
            logger.error("Daily sub-sector composite refresh failed: %s", e)

    def _refresh_prices_if_stale(self):
        """Run ~30s after dashboard startup. If historical_prices is more than
        1 calendar day stale (e.g. because the 14:00 UTC cron didn't fire
        while the dashboard was offline), trigger an immediate refresh so the
        user has fresh data within ~5-10 min of starting the dashboard,
        without having to wait for the next scheduled firing.

        Uses Tencent (0700.HK) as the freshness probe — a liquid name that
        always trades when the HK market is open, so its latest bar is a
        reliable proxy for the table's overall freshness."""
        from storage.factory import get_prices_repo
        try:
            prices_repo = get_prices_repo(self._securities_repo.db)
            probe_ticker = "0700.HK"
            latest_str = (prices_repo.latest_date(probe_ticker)
                            if hasattr(prices_repo, "latest_date") else None)
            if not latest_str:
                logger.info("Startup freshness check: no bars for %s — "
                            "triggering daily refresh", probe_ticker)
                self._refresh_historical_prices_daily()
                return
            try:
                latest = date.fromisoformat(latest_str[:10])
            except ValueError:
                logger.warning("Startup freshness check: unparseable date %r — "
                                "skipping refresh", latest_str)
                return
            days_stale = (date.today() - latest).days
            if days_stale <= 1:
                logger.info("Startup freshness check: %s last bar %s (%d days "
                            "stale) — fresh enough, skipping",
                            probe_ticker, latest, days_stale)
                return
            logger.info("Startup freshness check: %s last bar %s (%d days "
                        "stale) — triggering daily refresh now",
                        probe_ticker, latest, days_stale)
            self._refresh_historical_prices_daily()
        except Exception as e:
            logger.error("Startup freshness check failed: %s", e)

    def _reoptimize_parameters(self):
        """Monthly walk-forward CV re-optimization for each non-distress screen.
        Persists per-(screen, industry) best params to optimized_parameters."""
        import sqlite3
        from datetime import date as date_cls
        from storage.repository import OptimizedParamsRepository
        from analysis.optimization import WalkForwardOptimizer
        from analysis.screens import BUILTIN_SCREENS
        try:
            repo = OptimizedParamsRepository(self._securities_repo.db)
            optimizer = WalkForwardOptimizer(
                self._securities_repo.db.db_path,
                sector_risk_path=str(self._config.BASE_DIR / "config" / "sector_risk.yaml"),
            )
            # Get industries with enough coverage to optimize
            with sqlite3.connect(self._securities_repo.db.db_path) as conn:
                rows = conn.execute("""
                    SELECT yf_sector, COUNT(DISTINCT ticker) AS n
                    FROM securities WHERE is_active=1 AND yf_sector IS NOT NULL
                    GROUP BY yf_sector HAVING n >= 10 ORDER BY yf_sector
                """).fetchall()
                industries = [r[0] for r in rows]

            end = date_cls.today().strftime("%Y-%m-%d")
            for screen in BUILTIN_SCREENS:
                logger.info("Re-optimizing screen=%s across %d industries", screen.id, len(industries))
                optimizer.optimize_all_industries(
                    screen, industries, start_date="2018-01-01", end_date=end,
                    persist_repo=repo,
                )
        except Exception as e:
            logger.error("Monthly parameter re-optimization failed: %s", e)
