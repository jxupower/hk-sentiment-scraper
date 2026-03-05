import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent

# Paths
DB_PATH = str(BASE_DIR / "data" / "sentiment.db")
WATCHLIST_PATH = str(BASE_DIR / "config" / "watchlist.yaml")
RSS_FEEDS_PATH = str(BASE_DIR / "config" / "rss_feeds.yaml")

# Scraping
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "30"))
MAX_ARTICLES_PER_SOURCE = int(os.getenv("MAX_ARTICLES_PER_SOURCE", "50"))
SENTIMENT_HISTORY_DAYS = int(os.getenv("SENTIMENT_HISTORY_DAYS", "90"))

# API credentials (all optional)
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "SentimentScraper/1.0")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")

# Dashboard
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8050"))
DASHBOARD_DEBUG = os.getenv("DASHBOARD_DEBUG", "false").lower() == "true"


def _normalize_entry(entry) -> dict:
    """Accept either a plain string ticker or a {ticker, name, aliases} dict."""
    if isinstance(entry, str):
        return {"ticker": entry, "name": entry, "aliases": []}
    return {
        "ticker": entry["ticker"],
        "name": entry.get("name", entry["ticker"]),
        "aliases": entry.get("aliases", []),
    }


def load_watchlist() -> dict:
    with open(WATCHLIST_PATH) as f:
        data = yaml.safe_load(f)
    s = data.get("settings", {})
    global SCRAPE_INTERVAL_MINUTES, MAX_ARTICLES_PER_SOURCE, SENTIMENT_HISTORY_DAYS
    SCRAPE_INTERVAL_MINUTES = s.get("default_scrape_interval_minutes", SCRAPE_INTERVAL_MINUTES)
    MAX_ARTICLES_PER_SOURCE = s.get("max_articles_per_source", MAX_ARTICLES_PER_SOURCE)
    SENTIMENT_HISTORY_DAYS = s.get("sentiment_history_days", SENTIMENT_HISTORY_DAYS)
    # Normalize all entries
    normalized = {}
    for sector, entries in data.get("sectors", {}).items():
        normalized[sector] = [_normalize_entry(e) for e in entries]
    data["sectors"] = normalized
    return data


def get_all_tickers(watchlist: dict) -> list[str]:
    """Return all ticker symbols across all sectors."""
    return [e["ticker"] for entries in watchlist["sectors"].values() for e in entries]


def get_all_entries(watchlist: dict) -> list[dict]:
    """Return all {ticker, name, aliases, sector} dicts."""
    result = []
    for sector, entries in watchlist["sectors"].items():
        for e in entries:
            result.append({**e, "sector": sector})
    return result


def get_sector_for_ticker(ticker: str, watchlist: dict) -> str | None:
    for sector, entries in watchlist["sectors"].items():
        if any(e["ticker"] == ticker for e in entries):
            return sector
    return None


def get_tickers_for_sector(sector: str, watchlist: dict) -> list[str]:
    return [e["ticker"] for e in watchlist["sectors"].get(sector, [])]


_SECTOR_BROAD_TERMS: dict[str, list[str]] = {
    "Software Development": ["China tech", "Chinese tech", "China internet", "China platform",
                              "China e-commerce", "China digital", "China app", "China mobile internet",
                              "China gaming", "China AI", "China large language model"],
    "Communications": ["China Mobile", "China Telecom", "China Unicom", "China 5G",
                       "China telecom", "China telecommunications", "China wireless network"],
    "IT Hardware": ["China PC", "China smartphone", "China hardware",
                    "China electronics", "China consumer electronics"],
    "Computer Transistors": ["China semiconductor", "China chip", "China foundry",
                              "China chipmaker", "China wafer", "China IC design",
                              "China integrated circuit"],
    "Banking": ["China bank", "HK bank", "Hong Kong bank", "China banking",
                "Chinese bank", "China lender", "China credit", "China loan"],
    "Finance": ["HKEX", "Hong Kong exchange", "China fintech", "China brokerage",
                "China investment bank", "China financial services", "China wealth management"],
    "Insurance": ["China insurance", "HK insurance", "Chinese insurance",
                  "China life insurance", "China insurer"],
    "Real Estate": ["China property", "Hong Kong property", "HK property",
                    "China real estate", "China developer", "China housing",
                    "China land", "property market", "China home sales"],
    "Property Management": ["China property management", "HK commercial property",
                             "Hong Kong REIT", "China commercial real estate"],
    "Healthcare": ["China healthcare", "China hospital", "China medical", "China health",
                   "China drug distribution", "China medical device"],
    "Pharmaceuticals": ["China pharma", "China pharmaceutical", "China drug",
                        "China medicine", "China generic drug", "China prescription"],
    "Biotech": ["China biotech", "China biologic", "China oncology",
                "China clinical trial", "China biopharma", "China cell therapy"],
    "Industrial Machinery": ["power tools", "China machinery", "China power tools",
                              "China industrial equipment", "China manufacturing equipment"],
    "Textiles": ["China textile", "China apparel", "China garment", "China clothing",
                 "China knitwear", "China fashion manufacturing"],
    "Education & Support Services": ["China education", "China tutoring", "China school",
                                      "China learning", "China EdTech", "China training"],
    "Leisure & Luxury": ["China luxury", "Macau casino", "Macau gaming",
                         "China tourism", "China travel", "China jewellery",
                         "China jewelry", "Macau gambling revenue", "China leisure",
                         "China hotpot", "China dining"],
    "Oil & Gas": ["China oil", "China gas", "China petroleum", "China crude",
                  "China LNG", "China refin", "China offshore oil"],
    "Coal": ["China coal", "China coal mining", "China coal production", "China thermal coal",
             "China coking coal", "China coal price"],
    "Chemicals": ["China chemical", "China fertilizer", "China petrochemical",
                  "China specialty chemical", "China chemical industry"],
    "Construction Materials": ["China cement", "China building material", "China construction material",
                                "China concrete", "China glass fiber"],
    "Mining": ["China mining", "China gold mining", "China copper mining", "China lithium",
               "China mineral", "China rare earth", "China molybdenum"],
    "Utilities": ["China power", "China electricity", "China utility", "China nuclear",
                  "China renewable", "China grid", "China gas supply", "Hong Kong utility",
                  "HK MTR", "Hong Kong transport infrastructure"],
}


def build_search_terms(watchlist: dict) -> dict[str, list[str]]:
    """Returns {ticker: [name, alias1, alias2, ...]} for article text matching.

    Each ticker also inherits broad sector-level keywords so general China/HK
    market articles are captured even when no specific company name is mentioned.
    """
    result = {}
    for sector, entries in watchlist["sectors"].items():
        broad = _SECTOR_BROAD_TERMS.get(sector, [])
        for e in entries:
            terms = [e["name"]] + e["aliases"] + broad
            result[e["ticker"]] = terms
    return result


def load_rss_feeds() -> list[dict]:
    with open(RSS_FEEDS_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("feeds", [])


def reddit_configured() -> bool:
    return bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET)


def claude_configured() -> bool:
    return bool(CLAUDE_API_KEY)
