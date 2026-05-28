from datetime import datetime
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
        if self._securities_repo is not None and self._fundamentals_repo is not None:
            self._scheduler.add_job(
                func=self._refresh_fundamentals,
                trigger="cron",
                hour=3,
                minute=15,  # offset from db_prune at 03:00
                id="fundamentals_refresh",
                max_instances=1,
            )
        # Backtest infrastructure refresh — only registered if securities_repo wired
        if self._securities_repo is not None:
            self._scheduler.add_job(
                func=self._refresh_historical_prices,
                trigger="cron",
                day_of_week="sun",
                hour=4,
                id="historical_prices_refresh",
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

    def run_once(self):
        self._scrape_and_analyze()

    def _build_dynamic_terms(self):
        """Pull fresh search terms from the securities table each cycle.

        Returns (full_terms, watchlist_terms, watchlist_tickers).
        Falls back to the static YAML-derived terms if no securities_repo is wired.
        """
        if self._securities_repo is None:
            return self._search_terms, self._search_terms, set(self._search_terms.keys())
        rows = self._securities_repo.get_all_active()
        full_terms = self._config.build_search_terms_from_db(rows, watchlist_only=False)
        watchlist_terms = self._config.build_search_terms_from_db(rows, watchlist_only=True)
        return full_terms, watchlist_terms, set(watchlist_terms.keys())

    def _scrape_and_analyze(self):
        from scrapers.rss_scraper import RssScraper

        logger.info("=== Scrape cycle started at %s ===", datetime.utcnow().isoformat())
        full_terms, watchlist_terms, watchlist_tickers = self._build_dynamic_terms()
        logger.info("Search terms: %d total, %d watchlist", len(full_terms), len(watchlist_terms))

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

        # Per-ticker signals (used for drill-down)
        for ticker in self._all_tickers:
            try:
                scores_24h = self._sentiment_repo.get_scores_for_ticker(ticker, hours=24)
                scores_7d = self._sentiment_repo.get_scores_for_ticker(ticker, hours=168)
                price_df = self._yahoo.fetch_price_history(ticker, period="1mo")
                sector = self._config.get_sector_for_ticker(ticker, self._watchlist)
                sig = self._signal_gen.compute_ticker_signal(
                    ticker=ticker, sector=sector,
                    scores_24h=scores_24h, scores_7d=scores_7d, price_df=price_df,
                )
                self._signal_repo.upsert_signal(
                    ticker=sig.ticker, sector=sig.sector,
                    avg_sentiment_24h=sig.avg_sentiment_24h,
                    avg_sentiment_7d=sig.avg_sentiment_7d,
                    article_count_24h=sig.article_count_24h,
                    price_momentum_5d=sig.price_momentum_5d,
                    signal=sig.signal, confidence=sig.confidence,
                )
            except Exception as e:
                logger.error("Ticker signal failed [%s]: %s", ticker, e)

        # Sector-level signals (primary dashboard view)
        for sector, entries in self._watchlist["sectors"].items():
            try:
                tickers = [e["ticker"] for e in entries]
                scores_24h = self._sentiment_repo.get_scores_for_sector(tickers, hours=24)
                scores_7d = self._sentiment_repo.get_scores_for_sector(tickers, hours=168)
                price_dfs = {t: self._yahoo.fetch_price_history(t, period="1mo") for t in tickers}
                sector_sig = self._sector_signal_gen.compute_sector_signal(
                    sector=sector,
                    scores_24h=scores_24h,
                    scores_7d=scores_7d,
                    price_dfs=price_dfs,
                )
                self._sector_signal_repo.insert_signal(
                    sector=sector_sig.sector,
                    avg_sentiment_24h=sector_sig.avg_sentiment_24h,
                    avg_sentiment_7d=sector_sig.avg_sentiment_7d,
                    article_count_24h=sector_sig.article_count_24h,
                    avg_price_momentum=sector_sig.avg_price_momentum,
                    direction=sector_sig.direction,
                    confidence=sector_sig.confidence,
                )
                logger.info("Sector [%s]: %s (sentiment=%.3f, momentum=%.2f%%)",
                            sector, sector_sig.direction,
                            sector_sig.avg_sentiment_24h, sector_sig.avg_price_momentum)
            except Exception as e:
                logger.error("Sector signal failed [%s]: %s", sector, e)

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

    def _refresh_fundamentals(self):
        """Daily yfinance .info pull for the universe. Skips tickers that already have
        a snapshot for today, so a partial overnight run resumes naturally."""
        from scrapers.fundamentals_scraper import FundamentalsScraper
        try:
            tickers = [s["ticker"] for s in self._securities_repo.get_universe()]
            scraper = FundamentalsScraper(throttle_seconds=1.5)
            scraper.fetch_many(tickers, self._fundamentals_repo)
        except Exception as e:
            logger.error("Daily fundamentals refresh failed: %s", e)

    def _refresh_historical_prices(self):
        """Weekly yfinance multi-year price history refresh — keeps the backtest
        engine's data fresh as new trading days land."""
        from storage.repository import HistoricalPricesRepository
        from scrapers.historical_price_scraper import fetch_many as price_fetch_many
        try:
            tickers = [s["ticker"] for s in self._securities_repo.get_universe()]
            prices_repo = HistoricalPricesRepository(self._securities_repo.db)
            # Only need the last year of new data; UPSERT handles dup dates.
            price_fetch_many(tickers, prices_repo, period="1y")
        except Exception as e:
            logger.error("Weekly historical price refresh failed: %s", e)

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
                if screen.id == "avoid_distress":
                    continue
                logger.info("Re-optimizing screen=%s across %d industries", screen.id, len(industries))
                optimizer.optimize_all_industries(
                    screen, industries, start_date="2018-01-01", end_date=end,
                    persist_repo=repo,
                )
        except Exception as e:
            logger.error("Monthly parameter re-optimization failed: %s", e)
