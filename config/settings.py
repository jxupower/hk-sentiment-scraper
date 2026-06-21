import json
import os
import re
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent

# Paths
DB_PATH = str(BASE_DIR / "data" / "sentiment.db")
WATCHLIST_PATH = str(BASE_DIR / "config" / "watchlist.yaml")
WATCHLIST_PATH_US = str(BASE_DIR / "config" / "watchlist_us.yaml")
RSS_FEEDS_PATH = str(BASE_DIR / "config" / "rss_feeds.yaml")
RSS_FEEDS_PATH_US = str(BASE_DIR / "config" / "rss_feeds_us.yaml")
HKEX_CACHE_DIR = BASE_DIR / "data" / "cache"

# External data sources
HKEX_LIST_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"

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

# Cloud DB (Supabase Postgres) — historical_prices + fundamentals_snapshots only.
# Articles / sentiment / signals / notes stay in local SQLite.
USE_CLOUD_DB = os.getenv("USE_CLOUD_DB", "false").lower() == "true"
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "")


def cloud_db_configured() -> bool:
    return bool(USE_CLOUD_DB and SUPABASE_DB_URL)


def _normalize_entry(entry) -> dict:
    """Accept either a plain string ticker or a {ticker, name, aliases, sub_sector?} dict.
    `sub_sector` is optional — when set, the reconciler uses it as the
    highest-priority source for resolving `securities.sub_sector`."""
    if isinstance(entry, str):
        return {"ticker": entry, "name": entry, "aliases": [], "sub_sector": None}
    return {
        "ticker": entry["ticker"],
        "name": entry.get("name", entry["ticker"]),
        "aliases": entry.get("aliases", []),
        "sub_sector": entry.get("sub_sector"),
    }


def load_watchlist(market: str = "HK") -> dict:
    """Load the watchlist YAML for the given market.

    HK uses `config/watchlist.yaml`; US uses `config/watchlist_us.yaml`.
    Falls back to an empty `sectors` dict if the file is missing (so the US
    path can ship before the YAML is curated).
    """
    path = WATCHLIST_PATH_US if (market or "HK").upper() == "US" else WATCHLIST_PATH
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {"sectors": {}}
    s = data.get("settings", {})
    # Only the HK YAML drives the global interval settings — US is additive.
    if (market or "HK").upper() == "HK":
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


def _get_sub_sector_map(watchlist: dict, securities_repo) -> dict[str, str]:
    """Return {ticker -> sub_sector} for every watchlist ticker.

    Resolution chain (matches universe/reconciler.py:_resolve_sub_sector):
      1. Watchlist YAML's per-entry `sub_sector` field (highest priority)
      2. The reconciler-populated `securities.sub_sector` column
    Tickers with no sub_sector at all get "Unclassified" so we don't drop
    them from the sentiment-tab grouping silently."""
    by_ticker_security = {s["ticker"]: s
                           for s in securities_repo.get_universe()}
    result: dict[str, str] = {}
    for entries in (watchlist.get("sectors") or {}).values():
        for entry in entries or []:
            t = entry.get("ticker")
            if not t:
                continue
            sub = (entry.get("sub_sector")
                    or by_ticker_security.get(t, {}).get("sub_sector")
                    or "Unclassified")
            result[t] = sub
    return result


def get_subsectors_for_sentiment(watchlist: dict, securities_repo) -> list[str]:
    """Distinct sub-sectors across the watchlist roster. Drives the Sentiment
    tab's sector cards (post the 2026-06 watchlist-UI removal — sentiment
    now buckets by sub_sector rather than the editorial watchlist sector)."""
    return sorted(set(_get_sub_sector_map(watchlist, securities_repo).values()))


def get_tickers_for_subsector(sub_sector: str, watchlist: dict,
                                securities_repo,
                                market: str | None = None) -> list[str]:
    """Watchlist tickers whose resolved sub_sector matches. Used by the
    Sentiment tab's per-bucket score queries + by job_runner when computing
    sub_sector-level sector_signals. When `market` is set, the result is
    additionally filtered to tickers belonging to that market — needed so
    the Sentiment tab doesn't blend HK + US scores under one sub-sector
    name like 'Banks'."""
    from utils.market import market_of_ticker
    smap = _get_sub_sector_map(watchlist, securities_repo)
    tickers = [t for t, s in smap.items() if s == sub_sector]
    if market is not None:
        m = market.upper()
        tickers = [t for t in tickers if market_of_ticker(t) == m]
    return tickers


def get_subsector_for_ticker(ticker: str, watchlist: dict,
                               securities_repo) -> str | None:
    """Single ticker → sub_sector lookup (same resolution chain as above)."""
    return _get_sub_sector_map(watchlist, securities_repo).get(ticker)


# ============================================================================
# 中文 display-label helpers — purely cosmetic, no resolver behaviour.
# Backends store English labels as canonical values; these functions only
# translate at the display boundary when `lang == "zh"`.
# ============================================================================

_SUB_SECTORS_YAML_CACHE: dict | None = None


def _load_sub_sectors_yaml() -> dict:
    """Lazy-load `config/sub_sectors.yaml` once per process. The translation
    maps live in the same file as the resolver taxonomy so all sub-sector
    state stays co-located."""
    global _SUB_SECTORS_YAML_CACHE
    if _SUB_SECTORS_YAML_CACHE is None:
        import os
        import yaml
        path = os.path.join(os.path.dirname(__file__), "sub_sectors.yaml")
        with open(path, "r", encoding="utf-8") as f:
            _SUB_SECTORS_YAML_CACHE = yaml.safe_load(f) or {}
    return _SUB_SECTORS_YAML_CACHE


def get_subsector_label(name: str | None, lang: str = "en") -> str:
    """Translate a sub-sector display label. Returns the input unchanged
    when `lang != "zh"`, when `name` is empty/None, or when no Chinese
    label is registered for that sub-sector (graceful fall-back keeps the
    English form visible rather than crashing)."""
    if lang != "zh" or not name:
        return name or ""
    cfg = _load_sub_sectors_yaml()
    return ((cfg.get("sub_sectors_zh") or {}).get(name)) or name


def get_sector_label(name: str | None, lang: str = "en") -> str:
    """Same as `get_subsector_label` but for the 11 parent sectors."""
    if lang != "zh" or not name:
        return name or ""
    cfg = _load_sub_sectors_yaml()
    return ((cfg.get("parent_sectors_zh") or {}).get(name)) or name


_SECTOR_BROAD_TERMS: dict[str, list[str]] = {
    "Platforms & Cloud Infrastructure": ["China tech", "Chinese tech", "China internet", "China platform",
                              "China e-commerce", "China digital", "China app", "China mobile internet",
                              "China gaming", "China AI", "China large language model",
                              "China cloud", "AliCloud", "Tencent Cloud", "Baidu Cloud"],
    "Telecom Services": ["China Mobile", "China Telecom", "China Unicom", "China 5G",
                          "China telecom", "China telecommunications", "China wireless network"],
    "Consumer Electronics & Devices": ["China PC", "China smartphone", "China hardware",
                                        "China electronics", "China consumer electronics",
                                        "Xiaomi", "Lenovo"],
    "Semiconductors & Equipment": ["China semiconductor", "China chip", "China foundry",
                              "China chipmaker", "China wafer", "China IC design",
                              "China integrated circuit", "semiconductor equipment"],
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
    "Drug Manufacturing": ["China pharma", "China pharmaceutical", "China drug",
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
    "Building Products & Equipment": ["China cement", "China building material", "China construction material",
                                       "China concrete", "China glass fiber"],
    "Mining": ["China mining", "China gold mining", "China copper mining", "China lithium",
               "China mineral", "China rare earth", "China molybdenum"],
    "Utilities": ["China power", "China electricity", "China utility", "China nuclear",
                  "China renewable", "China grid", "China gas supply", "Hong Kong utility",
                  "HK MTR", "Hong Kong transport infrastructure"],
}


_SECTOR_BROAD_TERMS_US: dict[str, list[str]] = {
    "Platforms & Cloud Infrastructure": ["US tech", "Big Tech", "Magnificent Seven",
                                          "FAANG", "MAANG", "US cloud", "AWS",
                                          "Azure", "Google Cloud", "US AI",
                                          "generative AI", "OpenAI", "ChatGPT"],
    "Semiconductors & Equipment": ["US chip", "American semiconductor",
                                    "US foundry", "US chipmaker",
                                    "CHIPS Act", "semiconductor equipment",
                                    "GPU", "datacenter chip", "AI chip"],
    "Application Software": ["US SaaS", "enterprise software", "American software"],
    "Consumer Electronics": ["US consumer electronics", "iPhone sales", "American hardware"],
    "Banks": ["US bank", "American bank", "US regional banks", "US lender",
              "Federal Reserve", "FDIC", "US banking", "money center bank"],
    "Capital Markets": ["Wall Street", "NYSE", "Nasdaq exchange", "US broker",
                          "US investment bank", "US asset manager"],
    "Credit Services": ["US payments", "American payments", "US fintech",
                         "Visa", "Mastercard"],
    "Insurance": ["US insurance", "American insurer", "P&C insurance"],
    "Health Insurance": ["US health insurance", "US health insurer",
                          "Medicare Advantage", "Medicaid managed care"],
    "Biotechnology": ["US biotech", "American biotech", "FDA approval",
                       "FDA trial", "Phase 3 trial", "biotech IPO"],
    "Drug Manufacturing": ["US pharma", "American pharmaceutical",
                            "US drugmaker", "drug pricing"],
    "Oil & Gas": ["US oil", "American oil", "US shale", "US natural gas",
                   "WTI crude", "Permian basin", "US LNG"],
    "Auto Manufacturers": ["US auto", "American carmaker", "Detroit Three",
                            "US EV", "US auto sales"],
    "Auto Tech": ["EV charging", "US electric vehicle", "Tesla",
                   "autonomous driving", "robotaxi"],
    "Retail (Defensive)": ["US retail", "American retailer", "US consumer spending"],
    "Packaged Foods & Beverages": ["US consumer staples", "American grocery",
                                     "US food", "US beverage"],
    "Media & Entertainment": ["US streaming", "Hollywood", "US box office",
                                "Netflix subscribers", "Disney+ subscribers"],
    "Telecom Services": ["US telecom", "US wireless", "American telecommunications",
                          "AT&T", "Verizon", "T-Mobile"],
    "Aerospace & Defense": ["US defense", "Pentagon contract", "US aerospace",
                              "Boeing", "Lockheed", "US military aircraft"],
    "Real Estate Developers": ["US homebuilder", "US housing market",
                                 "American homebuilder"],
    "Diversified Real Estate": ["US REIT", "US commercial real estate"],
    "Regulated Electric Utilities": ["US utility", "American power utility",
                                       "US grid"],
    "Renewable Utilities": ["US renewable energy", "US solar", "US wind",
                              "IRA tax credit"],
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


def load_rss_feeds(market: str = "HK") -> list[dict]:
    """Load the RSS feeds YAML for the given market. Falls back to empty
    list if the US YAML is missing so the scraper can ship before the
    feeds are curated."""
    path = RSS_FEEDS_PATH_US if (market or "HK").upper() == "US" else RSS_FEEDS_PATH
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return []
    return data.get("feeds", [])


# HKEX names often have suffixes that won't match real news (BABA-W, JD-SW, MEITUAN-W, etc.).
# Strip common share-class / RMB-counter / weighted-listing suffixes for matching.
_HKEX_NAME_SUFFIX_RE = re.compile(r"[\s\-]+(?:S?W[BR]?|R|S|H|A|N|U|P|Z)\s*$", re.IGNORECASE)


def clean_hkex_name(name: str) -> str:
    """Strip HKEX trailing share-class / counter suffixes so the name matches news copy."""
    if not name:
        return ""
    cleaned = _HKEX_NAME_SUFFIX_RE.sub("", name).strip()
    return cleaned


def build_search_terms_from_db(securities_rows: list[dict],
                               watchlist_only: bool = False,
                               market: str = "HK") -> dict[str, list[str]]:
    """Build {ticker: [terms]} from the securities table.

    For watchlist tickers, terms = aliases_json + sector broad terms (sourced
    from the market-appropriate broad-terms dict).
    For universe tickers, terms = [cleaned_name] (clean_hkex_name strips
    HKEX suffixes; for US rows the name passes through unchanged).
    """
    market = (market or "HK").upper()
    broad_terms_map = (_SECTOR_BROAD_TERMS_US if market == "US"
                        else _SECTOR_BROAD_TERMS)
    result: dict[str, list[str]] = {}
    for row in securities_rows:
        ticker = row["ticker"]
        if row.get("is_watchlist"):
            try:
                aliases = json.loads(row.get("aliases_json") or "[]")
            except json.JSONDecodeError:
                aliases = []
            sector = row.get("watchlist_sector") or ""
            broad = broad_terms_map.get(sector, [])
            terms = list(aliases) if aliases else [clean_hkex_name(row["name"])]
            terms.extend(broad)
            result[ticker] = [t for t in terms if t]
        elif not watchlist_only:
            # clean_hkex_name() is a no-op for US tickers (no .HK suffix /
            # share-class artefact to strip), so the same helper works for both.
            cleaned = clean_hkex_name(row["name"])
            if cleaned:
                result[ticker] = [cleaned]
    return result


def reddit_configured() -> bool:
    return bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET)


def claude_configured() -> bool:
    return bool(CLAUDE_API_KEY)
