import time
import feedparser
from utils.helpers import clean_text, extract_ticker_hints, normalize_datetime
from utils.logger import get_logger
from scrapers.base_scraper import BaseScraper, RawArticle

logger = get_logger(__name__)


class RssScraper(BaseScraper):
    def __init__(self, feed_configs: list[dict]):
        self.feed_configs = feed_configs

    def fetch(self, search_terms: dict[str, list[str]]) -> list[RawArticle]:
        articles = []
        for feed_cfg in self.feed_configs:
            name = feed_cfg.get("name", feed_cfg["url"])
            url = feed_cfg["url"]
            try:
                parsed = feedparser.parse(url)
                count = 0
                for entry in parsed.entries:
                    article = self._parse_entry(entry, name, search_terms)
                    if article and article.ticker_hints:
                        articles.append(article)
                        count += 1
                logger.info("RSS [%s]: %d relevant articles", name, count)
            except Exception as e:
                logger.warning("RSS feed error [%s]: %s", name, e)
            time.sleep(0.5)
        return articles

    def _parse_entry(self, entry, feed_name: str,
                     search_terms: dict[str, list[str]]) -> RawArticle | None:
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

        hints = extract_ticker_hints(f"{title} {body}", search_terms)
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
