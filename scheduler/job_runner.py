from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from utils.logger import get_logger

logger = get_logger(__name__)


class JobRunner:
    def __init__(self, config, scrapers, analyzer, signal_gen, sector_signal_gen,
                 article_repo, sentiment_repo, signal_repo, sector_signal_repo,
                 yahoo_scraper, search_terms, all_tickers, watchlist, interval_minutes=30):
        self._config = config
        self._scrapers = scrapers
        self._analyzer = analyzer
        self._signal_gen = signal_gen
        self._sector_signal_gen = sector_signal_gen
        self._article_repo = article_repo
        self._sentiment_repo = sentiment_repo
        self._signal_repo = signal_repo
        self._sector_signal_repo = sector_signal_repo
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
        self._scheduler.start()
        logger.info("Scheduler started. Scraping every %d minutes.", self._interval)

    def stop(self):
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")

    def run_once(self):
        self._scrape_and_analyze()

    def _scrape_and_analyze(self):
        logger.info("=== Scrape cycle started at %s ===", datetime.utcnow().isoformat())
        all_articles = []
        for scraper in self._scrapers:
            if not scraper.is_available():
                continue
            try:
                articles = scraper.fetch(self._search_terms)
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
