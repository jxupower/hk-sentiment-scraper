from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import pandas as pd
from utils.helpers import clean_text, normalize_datetime
from utils.logger import get_logger
from utils.rate_limiter import get_shared_limiter
from utils.ticker_matcher import TickerMatcher
from scrapers.base_scraper import BaseScraper, RawArticle

logger = get_logger(__name__)

# Per-ticker Yahoo fetches are I/O-bound (network wait releases the GIL),
# so 8 workers give a ~8× wall-clock reduction. The `HostRateLimiter`
# enforces the same 0.5 s minimum-interval-per-host as the old sync
# code, so Yahoo's rate limits remain respected — we just stop wasting
# time sleeping when a *different* host could be answering.
_YAHOO_WORKERS = 8
_YAHOO_HOST = "yfinance.yahoo.com"


class YahooScraper(BaseScraper):
    def fetch(self, search_terms: dict[str, list[str]]) -> list[RawArticle]:
        """search_terms: {ticker: [name, alias1, ...]}.

        Per Phase 3, this is called with watchlist-only terms — Yahoo per-ticker news
        does not scale to the full HKEX universe.
        """
        from config.settings import load_universe_aliases
        short_allow = set(load_universe_aliases().get("short_allow") or [])
        matcher = TickerMatcher(search_terms, set(search_terms.keys()),
                                     short_term_allowlist=short_allow)
        limiter = get_shared_limiter()
        tickers = list(search_terms.keys())
        articles = []

        def _fetch_one(ticker: str):
            try:
                # Per-host throttle. All workers targeting Yahoo serialise
                # on the SAME lock (0.5 s between successive calls). Two
                # workers pointed at different hosts don't block each
                # other — but here every call is Yahoo, so the effective
                # ceiling is 1 fetch per 0.5 s wall-clock (2/s), and the
                # 8-worker parallelism absorbs the per-call ~0.3-0.8 s
                # network latency without adding throttle wait time.
                limiter.wait(_YAHOO_HOST)
                items = self._get_news(ticker, matcher)
                return ticker, items, None
            except Exception as e:
                return ticker, [], e

        with ThreadPoolExecutor(max_workers=_YAHOO_WORKERS,
                                  thread_name_prefix="yahoo") as pool:
            futures = [pool.submit(_fetch_one, t) for t in tickers]
            for fut in as_completed(futures):
                ticker, items, err = fut.result()
                if err is not None:
                    logger.warning("Yahoo news error [%s]: %s", ticker, err)
                else:
                    articles.extend(items)
                    logger.info("Yahoo [%s]: %d articles", ticker, len(items))
        return articles

    def _get_news(self, ticker: str, matcher: TickerMatcher) -> list[RawArticle]:
        t = yf.Ticker(ticker)
        articles = []
        try:
            news = t.news or []
        except Exception:
            return []

        for item in news:
            content = item.get("content", {})
            title = clean_text(content.get("title", "") or item.get("title", ""))
            url = (content.get("canonicalUrl", {}) or {}).get("url", "") or item.get("link", "")

            if not title or not url:
                continue

            summary = clean_text(content.get("summary", "") or "")
            hints = matcher.match(f"{title} {summary}", max_tags=5)
            # Always tag the queried ticker since Yahoo returned this article for it
            if ticker not in hints:
                hints.append(ticker)

            pub_ts = content.get("pubDate") or item.get("providerPublishTime")
            published_at = normalize_datetime(pub_ts) if isinstance(pub_ts, (int, float)) else None
            provider = content.get("provider", {})
            author = provider.get("displayName") if isinstance(provider, dict) else None

            articles.append(RawArticle(
                source="yahoo",
                title=title,
                body=summary,
                url=url,
                ticker_hints=hints,
                published_at=published_at,
                author=author,
            ))
        return articles

    def fetch_price_history(self, ticker: str, period: str = "3mo") -> pd.DataFrame:
        try:
            t = yf.Ticker(ticker)
            df = t.history(period=period)
            return df
        except Exception as e:
            logger.warning("Yahoo price error [%s]: %s", ticker, e)
            return pd.DataFrame()
