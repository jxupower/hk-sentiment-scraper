import time
from typing import Optional
from utils.helpers import clean_text, extract_ticker_hints, normalize_datetime
from utils.logger import get_logger
from scrapers.base_scraper import BaseScraper, RawArticle

logger = get_logger(__name__)

# Subreddits relevant to China/HK market coverage
SUBREDDITS = ["investing", "stocks", "GlobalMarkets", "SecurityAnalysis", "chinastocks"]


class RedditScraper(BaseScraper):
    def __init__(self, client_id: str, client_secret: str, user_agent: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._user_agent = user_agent
        self._reddit = None

    def is_available(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _get_reddit(self):
        if self._reddit is None:
            import praw
            self._reddit = praw.Reddit(
                client_id=self._client_id,
                client_secret=self._client_secret,
                user_agent=self._user_agent,
                check_for_async=False,
            )
        return self._reddit

    def fetch(self, search_terms: dict[str, list[str]]) -> list[RawArticle]:
        """search_terms: {ticker: [name, alias1, ...]}"""
        if not self.is_available():
            logger.info("Reddit scraper disabled (no credentials). See setup_guide.md.")
            return []

        # Build a flat list of company names to search Reddit (not ticker symbols)
        company_names = []
        for terms in search_terms.values():
            if terms:
                company_names.append(terms[0])  # primary name

        articles = []
        reddit = self._get_reddit()
        for sub_name in SUBREDDITS:
            try:
                sub_articles = self._fetch_subreddit(reddit, sub_name, search_terms, company_names)
                articles.extend(sub_articles)
                logger.info("Reddit [r/%s]: %d relevant posts", sub_name, len(sub_articles))
                time.sleep(1)
            except Exception as e:
                logger.warning("Reddit error [r/%s]: %s", sub_name, e)
        return articles

    def _fetch_subreddit(self, reddit, sub_name: str,
                         search_terms: dict[str, list[str]],
                         company_names: list[str]) -> list[RawArticle]:
        subreddit = reddit.subreddit(sub_name)
        articles = []
        seen_urls = set()

        # Hot posts — filter by name/alias matching
        for post in subreddit.hot(limit=50):
            article = self._post_to_article(post, search_terms)
            if article and article.url not in seen_urls and article.ticker_hints:
                articles.append(article)
                seen_urls.add(article.url)

        # Search by company name (more reliable than ticker symbols on Reddit)
        for name in company_names[:15]:  # cap to avoid rate limits
            try:
                for post in subreddit.search(name, limit=10, sort="new"):
                    article = self._post_to_article(post, search_terms)
                    if article and article.url not in seen_urls and article.ticker_hints:
                        articles.append(article)
                        seen_urls.add(article.url)
                time.sleep(0.5)
            except Exception:
                pass

        return articles

    def _post_to_article(self, post,
                         search_terms: dict[str, list[str]]) -> Optional[RawArticle]:
        try:
            title = clean_text(post.title or "")
            body = clean_text(post.selftext or "")
            url = f"https://reddit.com{post.permalink}"
            if not title:
                return None

            hints = extract_ticker_hints(f"{title} {body}", search_terms)

            return RawArticle(
                source="reddit",
                title=title,
                body=body[:1000],
                url=url,
                ticker_hints=hints,
                published_at=normalize_datetime(post.created_utc),
                author=str(post.author) if post.author else None,
                raw_score=float(post.score),
            )
        except Exception:
            return None
