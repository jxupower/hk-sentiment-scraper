"""Scrape Wikipedia constituent lists for the major HK + US indices into
`config/index_constituents.yaml`.

Why this script: the Market tab (dashboard/market_layout.py) needs to know
which tickers belong to ^HSI, ^GSPC, etc. so it can filter the constituent
table. No existing data source in the codebase ships that mapping — yfinance's
`Ticker.info['components']` is unreliable per-symbol, and the universe table
just lists every active equity per market, not which index it belongs to.

Each index entry parses the Wikipedia article's first usable table — the
ticker column lives in slightly different positions per page so each entry
specifies how to find + normalise it. Normalisation per market:

  HK  : Wikipedia uses bare codes like "1" or "388"; we pad to 4 digits and
        suffix `.HK` (so "5" → "0005.HK", matching our DB).
  US  : Wikipedia uses dotted share classes like "BRK.B" — we swap to the
        yfinance hyphen form "BRK-B". Plain tickers pass through.

Usage:

    venv\\Scripts\\python scripts\\refresh_index_constituents.py
        [--out config/index_constituents.yaml]
        [--only ^HSI,^GSPC]            # subset for quick re-runs

Output schema:

    ^HSI:
      name: Hang Seng Index
      market: HK
      source_url: https://en.wikipedia.org/wiki/Hang_Seng_Index
      updated: 2026-06-27
      tickers: [0005.HK, 0001.HK, ...]
    ^GSPC: { ... }

Idempotent — re-running overwrites the YAML cleanly. CI-safe (no auth).

Caveats:
  * Wikipedia table layouts shift occasionally. We warn when an index parses
    fewer than ~80% of its expected row count (`expected_min` per entry) so
    silent breakage is visible to the operator.
  * Russell 2000 (^RUT) and NASDAQ Composite (^IXIC) are NOT scraped here —
    they have 2000 / 3000+ members and Wikipedia doesn't host the full
    lists. The Market tab degrades gracefully: chart still renders, table
    shows "constituent list not maintained for this index."
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import requests
import yaml

# Make project root importable when running this file directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# Per-index scraper config
# --------------------------------------------------------------------------

@dataclass
class IndexSpec:
    symbol: str
    name: str
    market: str                                  # "HK" | "US"
    source_url: str
    ticker_col_candidates: list[str]             # tried in order
    table_picker: Optional[Callable[[list[pd.DataFrame]], pd.DataFrame]] = None
    expected_min: int = 1                        # warning threshold for sanity


def _pick_table_with_col(cols: list[str]) -> Callable[[list[pd.DataFrame]], pd.DataFrame]:
    """Return a picker that selects the first table whose columns include
    any of the listed candidate column names (case-insensitive)."""
    targets = [c.lower() for c in cols]
    def picker(tables: list[pd.DataFrame]) -> pd.DataFrame:
        for t in tables:
            lc = [str(c).lower() for c in t.columns]
            for tgt in targets:
                if any(tgt in c for c in lc):
                    return t
        raise RuntimeError(f"No Wikipedia table contained any of {cols}")
    return picker


INDEXES: list[IndexSpec] = [
    IndexSpec(
        symbol="^GSPC",
        name="S&P 500",
        market="US",
        source_url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        ticker_col_candidates=["Symbol", "Ticker"],
        table_picker=_pick_table_with_col(["Symbol"]),
        expected_min=480,
    ),
    IndexSpec(
        symbol="^NDX",
        name="NASDAQ-100",
        market="US",
        source_url="https://en.wikipedia.org/wiki/Nasdaq-100",
        ticker_col_candidates=["Ticker", "Symbol"],
        table_picker=_pick_table_with_col(["Ticker", "Symbol"]),
        expected_min=90,
    ),
    IndexSpec(
        symbol="^DJI",
        name="Dow Jones Industrial Average",
        market="US",
        source_url="https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average",
        ticker_col_candidates=["Symbol", "Ticker"],
        table_picker=_pick_table_with_col(["Symbol"]),
        expected_min=28,
    ),
    IndexSpec(
        symbol="^HSI",
        name="Hang Seng Index",
        market="HK",
        source_url="https://en.wikipedia.org/wiki/Hang_Seng_Index",
        ticker_col_candidates=["Stock Code", "Ticker", "Code"],
        table_picker=_pick_table_with_col(["Stock Code", "Code", "Ticker"]),
        expected_min=70,
    ),
    # ^HSTECH (Hang Seng Tech) is intentionally NOT scraped here — there's
    # no dedicated Wikipedia article for it. The 30 constituents are stable
    # enough to hand-curate; bundled directly in config/index_constituents.yaml
    # under the ^HSTECH key. Re-curate manually when the index reconstitutes
    # (HSI Indexes publishes the methodology change history).
    IndexSpec(
        symbol="^HSCEI",
        name="Hang Seng China Enterprises Index",
        market="HK",
        source_url="https://en.wikipedia.org/wiki/Hang_Seng_China_Enterprises_Index",
        ticker_col_candidates=["Stock Code", "Ticker", "Code"],
        table_picker=_pick_table_with_col(["Stock Code", "Code", "Ticker"]),
        expected_min=40,
    ),
]


# --------------------------------------------------------------------------
# Ticker normalisation
# --------------------------------------------------------------------------

def _normalise_hk(raw) -> Optional[str]:
    """HK Wikipedia codes: numeric strings like "5" or "388". → "0005.HK"."""
    if raw is None:
        return None
    s = str(raw).strip()
    # Strip anything non-numeric (some entries have footnote markers like "5[a]")
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    return f"{int(digits):04d}.HK"


def _normalise_us(raw) -> Optional[str]:
    """US Wikipedia tickers: mostly plain ("AAPL"). Share classes use dots
    ("BRK.B") → yfinance hyphen form ("BRK-B")."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s or s == "NAN":
        return None
    # Footnote-style suffix cleanup (e.g. "AAPL[1]")
    if "[" in s:
        s = s.split("[", 1)[0].strip()
    return s.replace(".", "-")


def _normalise(raw, market: str) -> Optional[str]:
    return _normalise_hk(raw) if market == "HK" else _normalise_us(raw)


# --------------------------------------------------------------------------
# Per-index scrape
# --------------------------------------------------------------------------

# Wikipedia 403s on the default urllib User-Agent that pandas.read_html uses;
# fetching via requests first with a real UA + handing the HTML to read_html
# avoids that.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36")


def _fetch_html(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def scrape_one(spec: IndexSpec) -> dict:
    print(f"[{spec.symbol}] fetching {spec.source_url}")
    html_text = _fetch_html(spec.source_url)
    # Wrap in StringIO — modern pandas treats a bare string arg as a path
    # rather than HTML content (deprecated path behaviour, was removed in
    # pandas 2.x). The StringIO is unambiguous.
    tables = pd.read_html(StringIO(html_text))
    print(f"  found {len(tables)} tables; picking by column ...")
    if spec.table_picker is None:
        target = tables[0]
    else:
        target = spec.table_picker(tables)

    # Find the actual ticker column in the chosen table.
    cols_lc = {str(c).lower(): c for c in target.columns}
    ticker_col = None
    for cand in spec.ticker_col_candidates:
        for lc, original in cols_lc.items():
            if cand.lower() in lc:
                ticker_col = original
                break
        if ticker_col is not None:
            break
    if ticker_col is None:
        raise RuntimeError(
            f"[{spec.symbol}] none of {spec.ticker_col_candidates} matched "
            f"columns {list(target.columns)}"
        )

    raws = target[ticker_col].tolist()
    tickers = []
    seen = set()
    for raw in raws:
        t = _normalise(raw, spec.market)
        if t and t not in seen:
            tickers.append(t)
            seen.add(t)
    print(f"  parsed {len(tickers)} unique tickers from col '{ticker_col}'")
    if len(tickers) < spec.expected_min:
        print(f"  WARNING: expected ≥{spec.expected_min}, got {len(tickers)} — "
                f"Wikipedia table layout may have changed.")

    return {
        "name": spec.name,
        "market": spec.market,
        "source_url": spec.source_url,
        "updated": date.today().isoformat(),
        "tickers": tickers,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="config/index_constituents.yaml",
                    help="Output YAML path (default: config/index_constituents.yaml)")
    p.add_argument("--only", default=None,
                    help="Comma-separated symbols to refresh (default: all).")
    args = p.parse_args()

    only = (set(s.strip() for s in args.only.split(",")) if args.only
              else None)

    # Preserve existing entries the operator didn't ask to refresh.
    out_path = ROOT / args.out
    existing: dict = {}
    if out_path.exists():
        try:
            existing = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            existing = {}

    results: dict = dict(existing)
    failed: list[str] = []
    for spec in INDEXES:
        if only and spec.symbol not in only:
            continue
        try:
            results[spec.symbol] = scrape_one(spec)
        except Exception as e:
            # Truncate the message and strip non-cp1252 chars so this print
            # never itself dies on Windows. The full HTML body would otherwise
            # spill several hundred KB of unicode through the console.
            msg = repr(e)[:200].encode("ascii", errors="replace").decode("ascii")
            print(f"[{spec.symbol}] FAILED: {type(e).__name__}: {msg}")
            failed.append(spec.symbol)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(results, sort_keys=False, allow_unicode=True,
                          default_flow_style=False),
        encoding="utf-8",
    )
    print()
    print(f"Wrote {len(results)} index entries to {out_path}")
    if failed:
        print(f"FAILED: {failed} (the YAML retains prior entries for these)")
        sys.exit(1)


if __name__ == "__main__":
    main()
