"""US universe loader.

Three acquisition paths, picked by `download_and_parse(source=...)`:

* **`nasdaqtrader`** (default, recommended). Free pipe-delimited symbol
  directory served by the NASDAQ Trader site. No auth, no rate limit, daily
  updates. Two files unioned:
    - `nasdaqlisted.txt`  — Nasdaq-listed common stock + ETFs
    - `otherlisted.txt`   — NYSE / NYSE American / NYSE Arca listings
  After filtering to common stock (ETF=N, Test Issue=N, name contains
  "Common Stock"), the union gives ~6,000-7,000 active US equities — a
  superset of Russell 3000 that the dashboard's market-cap screener can
  narrow down on demand. Tickers are normalised to yfinance form
  (`BRK B` / `BRK.B` -> `BRK-B`).

* **`wikipedia`**. Scrapes S&P 500 + Nasdaq-100 + Dow 30 union (~600
  unique). Use when NASDAQ Trader is unreachable or the user wants a
  smaller, mega-cap-only universe.

* **`ishares`**. Downloads iShares IWV (Russell 3000 ETF) holdings CSV.
  Kept for completeness; the iShares CDN frequently serves a marketing
  HTML page instead of the CSV when called without a session cookie, so
  it's NOT the default.

All paths return a list of dicts shaped exactly like `hkex_loader.parse()`:
    {ticker, hkex_code, name, listing_category, lot_size, yf_sector_hint?}

`hkex_code` is left empty for US rows — the schema migration in Phase 1
dropped the legacy NOT NULL constraint, so empty / NULL is fine.
`lot_size` is 100 for US equities by NASDAQ convention.
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

NASDAQLISTED_URL  = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHERLISTED_URL   = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

IWV_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund"
)

WIKI_SP500_URL     = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NASDAQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
WIKI_DOW30_URL     = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/vnd.ms-excel,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_BLACKLIST = {"-", "USD", "", "ZVZZT", "ZWZZT", "ZXZZT", "ZBZZT"}  # last 4 are NASDAQ test symbols


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------

def _normalize_us_ticker(raw: str) -> Optional[str]:
    """Canonicalise to yfinance form: `BRK.B` / `BRK B` -> `BRK-B`. Returns
    None for blank / blacklisted / test symbols."""
    if raw is None:
        return None
    t = str(raw).strip().upper()
    if not t or t in _BLACKLIST:
        return None
    # NASDAQ Trader: space-delimited share classes. yfinance: dash.
    # IWV / Wikipedia: dot-delimited. Canonicalise to dash.
    t = t.replace(" ", "-").replace(".", "-")
    return t


# ---------------------------------------------------------------------------
# Primary path — NASDAQ Trader symbol directory
# ---------------------------------------------------------------------------

def _download_text(url: str, cache_path: Path, force: bool = False) -> str:
    """Download a small text file with caching."""
    if cache_path.exists() and not force:
        logger.info("Using cached %s", cache_path)
        return cache_path.read_text(encoding="utf-8", errors="replace")
    logger.info("Downloading %s ...", url)
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=60)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(resp.text, encoding="utf-8")
    logger.info("Saved %s (%d bytes)", cache_path, len(resp.text))
    return resp.text


def parse_nasdaq_trader(cache_dir: Path, force: bool = False) -> list[dict]:
    """Union nasdaqlisted.txt + otherlisted.txt, filter to active common stock."""
    stamp = datetime.utcnow().strftime("%Y%m%d")
    nasdaq_path = cache_dir / f"nasdaqlisted_{stamp}.txt"
    other_path  = cache_dir / f"otherlisted_{stamp}.txt"
    nasdaq_text = _download_text(NASDAQLISTED_URL, nasdaq_path, force=force)
    other_text  = _download_text(OTHERLISTED_URL,  other_path,  force=force)

    records: list[dict] = []
    seen: set[str] = set()

    # ---- nasdaqlisted.txt ----
    # Format: Symbol|Security Name|Market Category|Test Issue|Financial Status|
    #         Round Lot Size|ETF|NextShares
    # Last line is "File Creation Time" — skip it.
    df_n = pd.read_csv(io.StringIO(nasdaq_text), sep="|")
    df_n = df_n[df_n["Symbol"].notna() & df_n["Symbol"].str.strip().ne("")]
    df_n = df_n[~df_n["Symbol"].astype(str).str.startswith("File Creation")]
    df_n = df_n[df_n["Test Issue"] == "N"]
    df_n = df_n[df_n["ETF"] == "N"]
    df_n = df_n[df_n["Security Name"].astype(str).str.contains("Common Stock", case=False, na=False)]
    for r in df_n.to_dict("records"):
        t = _normalize_us_ticker(r.get("Symbol"))
        if not t or t in seen:
            continue
        name = str(r.get("Security Name") or t).split(" - ")[0].strip()
        try:
            lot = int(r.get("Round Lot Size") or 100)
        except (TypeError, ValueError):
            lot = 100
        records.append({
            "ticker": t,
            "hkex_code": "",
            "name": name,
            "listing_category": "Equity",
            "lot_size": lot,
            "yf_sector_hint": None,
        })
        seen.add(t)
    logger.info("nasdaqlisted: %d common-stock records", len(records))

    # ---- otherlisted.txt ----
    # Format: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|
    #         Test Issue|NASDAQ Symbol
    df_o = pd.read_csv(io.StringIO(other_text), sep="|")
    df_o = df_o[df_o["ACT Symbol"].notna() & df_o["ACT Symbol"].str.strip().ne("")]
    df_o = df_o[~df_o["ACT Symbol"].astype(str).str.startswith("File Creation")]
    df_o = df_o[df_o["Test Issue"] == "N"]
    df_o = df_o[df_o["ETF"] == "N"]
    df_o = df_o[df_o["Security Name"].astype(str).str.contains("Common Stock", case=False, na=False)]
    for r in df_o.to_dict("records"):
        t = _normalize_us_ticker(r.get("ACT Symbol"))
        if not t or t in seen:
            continue
        name = str(r.get("Security Name") or t).split(" - ")[0].strip()
        try:
            lot = int(r.get("Round Lot Size") or 100)
        except (TypeError, ValueError):
            lot = 100
        records.append({
            "ticker": t,
            "hkex_code": "",
            "name": name,
            "listing_category": "Equity",
            "lot_size": lot,
            "yf_sector_hint": None,
        })
        seen.add(t)
    logger.info("otherlisted: %d total common-stock records after union", len(records))
    return records


# ---------------------------------------------------------------------------
# Secondary path — iShares IWV holdings CSV (often blocked by their CDN)
# ---------------------------------------------------------------------------

def download_iwv(cache_dir: Path, force: bool = False) -> Path:
    """Download iShares IWV holdings, cache to disk, return the path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"iwv_holdings_{datetime.utcnow().strftime('%Y%m%d')}.csv"
    if cache_path.exists() and not force:
        logger.info("Using cached IWV holdings at %s", cache_path)
        return cache_path
    logger.info("Downloading iShares IWV holdings ...")
    resp = requests.get(IWV_HOLDINGS_URL, headers=_BROWSER_HEADERS, timeout=60)
    resp.raise_for_status()
    cache_path.write_bytes(resp.content)
    logger.info("Saved IWV holdings CSV to %s (%d bytes)", cache_path, len(resp.content))
    return cache_path


def parse_iwv(csv_path: Path) -> list[dict]:
    """Parse the iShares IWV holdings CSV (header row auto-detected)."""
    text = csv_path.read_text(encoding="utf-8", errors="replace")
    # Defensive: iShares CDN sometimes serves a marketing HTML page instead
    # of the CSV. Detect and bail clearly.
    if text.lstrip().startswith("<"):
        raise ValueError(
            f"IWV CSV at {csv_path} looks like HTML — iShares CDN probably "
            "served the marketing page (CSV requires session cookie)."
        )
    lines = text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines)
         if line.startswith("Ticker,") and "Name" in line),
        None,
    )
    if header_idx is None:
        raise ValueError(
            f"IWV CSV at {csv_path} has no `Ticker,Name,...` header row "
            "— format may have changed."
        )
    df = pd.read_csv(csv_path, skiprows=header_idx)
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].isin(["Equity"])]
    records: list[dict] = []
    seen: set[str] = set()
    for row in df.to_dict("records"):
        t = _normalize_us_ticker(row.get("Ticker"))
        if not t or t in seen:
            continue
        name = str(row.get("Name") or t).strip()
        sector = row.get("Sector") or None
        records.append({
            "ticker": t,
            "hkex_code": "",
            "name": name,
            "listing_category": "Equity",
            "lot_size": 100,
            "yf_sector_hint": (str(sector).strip() if sector else None),
        })
        seen.add(t)
    logger.info("Parsed %d unique US equities from IWV", len(records))
    return records


# ---------------------------------------------------------------------------
# Tertiary path — Wikipedia
# ---------------------------------------------------------------------------

def _wiki_tables(url: str) -> list[pd.DataFrame]:
    """Fetch a Wikipedia article and parse all tables. Wraps the HTML text in
    io.StringIO because pandas.read_html in 2.x treats a bare string as a
    file path."""
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    return pd.read_html(io.StringIO(resp.text))


def parse_wikipedia_lists() -> list[dict]:
    """Union of S&P 500 + Nasdaq-100 + Dow 30 (~600 unique tickers)."""
    seen: set[str] = set()
    records: list[dict] = []

    def _add(ticker, name, sector):
        t = _normalize_us_ticker(ticker)
        if not t or t in seen:
            return
        records.append({
            "ticker": t,
            "hkex_code": "",
            "name": str(name).strip(),
            "listing_category": "Equity",
            "lot_size": 100,
            "yf_sector_hint": str(sector).strip() if sector else None,
        })
        seen.add(t)

    # S&P 500 — first table.
    try:
        sp = _wiki_tables(WIKI_SP500_URL)[0]
        for r in sp.to_dict("records"):
            _add(r.get("Symbol") or r.get("Ticker"),
                  r.get("Security") or r.get("Company"),
                  r.get("GICS Sector"))
        logger.info("Wikipedia S&P 500: %d unique so far", len(records))
    except Exception as e:
        logger.warning("S&P 500 fetch failed: %s", e)

    # Nasdaq-100 — first table with a Ticker/Symbol column.
    try:
        for tbl in _wiki_tables(WIKI_NASDAQ100_URL):
            cols = {str(c).strip() for c in tbl.columns}
            if "Ticker" in cols or "Symbol" in cols:
                tcol = "Ticker" if "Ticker" in cols else "Symbol"
                ncol = next((c for c in ("Company", "Security", "Name")
                             if c in cols), None)
                scol = next((c for c in ("GICS Sector", "Sector") if c in cols), None)
                for r in tbl.to_dict("records"):
                    _add(r.get(tcol), r.get(ncol) or "",
                         r.get(scol) if scol else None)
                break
        logger.info("Wikipedia Nasdaq-100: %d unique so far", len(records))
    except Exception as e:
        logger.warning("Nasdaq-100 fetch failed: %s", e)

    # Dow 30 — same scan.
    try:
        for tbl in _wiki_tables(WIKI_DOW30_URL):
            cols = {str(c).strip() for c in tbl.columns}
            if "Symbol" in cols or "Ticker" in cols:
                tcol = "Symbol" if "Symbol" in cols else "Ticker"
                ncol = next((c for c in ("Company", "Security") if c in cols), None)
                scol = next((c for c in ("Industry", "GICS Sector") if c in cols), None)
                for r in tbl.to_dict("records"):
                    _add(r.get(tcol), r.get(ncol) or "",
                         r.get(scol) if scol else None)
                break
        logger.info("Wikipedia Dow 30: %d unique so far", len(records))
    except Exception as e:
        logger.warning("Dow 30 fetch failed: %s", e)

    return records


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

def download_and_parse(cache_dir: Path, source: str = "nasdaqtrader") -> list[dict]:
    """High-level entry — picks source, falls back to Wikipedia on failure."""
    source = (source or "nasdaqtrader").lower()
    try:
        if source == "ishares":
            return parse_iwv(download_iwv(cache_dir))
        if source == "wikipedia":
            return parse_wikipedia_lists()
        return parse_nasdaq_trader(cache_dir)
    except Exception as e:
        logger.warning("Primary source `%s` failed (%s) — falling back to Wikipedia",
                        source, e)
        return parse_wikipedia_lists()
