import time
import feedparser
from utils.helpers import clean_text, normalize_datetime
from utils.logger import get_logger
from utils.ticker_matcher import TickerMatcher
from scrapers.base_scraper import BaseScraper, RawArticle

logger = get_logger(__name__)


class RssScraper(BaseScraper):
    def __init__(self, feed_configs: list[dict], watchlist_tickers: set[str] = None):
        self.feed_configs = feed_configs
        self.watchlist_tickers = watchlist_tickers or set()

    def fetch(self, search_terms: dict[str, list[str]]) -> list[RawArticle]:
        # Build the matcher once per fetch — compiling 3,000+ terms takes ~10ms
        # and per-article match becomes fast.
        matcher = TickerMatcher(search_terms, self.watchlist_tickers)
        articles = []
        for feed_cfg in self.feed_configs:
            name = feed_cfg.get("name", feed_cfg["url"])
            url = feed_cfg["url"]
            try:
                parsed = feedparser.parse(url)
                count = 0
                for entry in parsed.entries:
                    article = self._parse_entry(entry, name, matcher)
                    if article and article.ticker_hints:
                        articles.append(article)
                        count += 1
                logger.info("RSS [%s]: %d relevant articles", name, count)
            except Exception as e:
                logger.warning("RSS feed error [%s]: %s", name, e)
            time.sleep(0.5)
        return articles

    def _parse_entry(self, entry, feed_name: str,
                     matcher: TickerMatcher) -> RawArticle | None:
        url = getattr(entry, "link", None)
        title = clean_text(getattr(entry, "title", ""))
        if not url or not title:
            return None

        body_raw = ""
        if hasattr(entry, "summary"):
            body_raw = entry.summary
        elif hasattr(entry, "content"):
            body_raw = entry.content[0].get("value", "") if entry.content else ""
        body = clean_text(body_raw)

        hints = matcher.match(f"{title} {body}", max_tags=5)
        published_at = normalize_datetime(getattr(entry, "published_parsed", None))
        author = getattr(entry, "author", None)

        return RawArticle(
            source="rss",
            title=title,
            body=body,
            url=url,
            ticker_hints=hints,
            published_at=published_at,
            author=author,
        )
