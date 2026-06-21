"""Market-of-ticker helper.

A single source of truth for "which market does this ticker belong to" so
every caller agrees. Convention:

* `0700.HK`, `9988.HK`        -> HK    (HKEX-listed equities)
* `&HK:BANKS`, `&US:BANKS`    -> embedded in the synthetic prefix
* `^HSI`, `^HSCEI`, `^HSTECH` -> HK    (Hang Seng indices)
* `^GSPC`, `^IXIC`, `^DJI`,
  `^NDX`, `^RUT`, `^VIX`      -> US    (US indices)
* `@CORE`, `@CORE$OPT`        -> follow the saved portfolio's recorded market
                                  (callers pass `market=` from the portfolio
                                  row; this helper only inspects the ticker)
* everything else (e.g.
  `AAPL`, `BRK-B`, `BF.B`,
  `MSFT`, no dot or dot
  followed by a single
  letter)                     -> US    (NYSE/NASDAQ tickers)

The helper is intentionally pure-string — it never hits the DB. Callers that
want the authoritative market should `SELECT market FROM securities` instead.
"""
from __future__ import annotations

_HK_INDICES = {"^HSI", "^HSCEI", "^HSTECH"}
_US_INDICES = {"^GSPC", "^IXIC", "^DJI", "^NDX", "^RUT", "^VIX"}


def market_of_ticker(ticker: str | None) -> str:
    """Return 'HK' or 'US' based on the ticker convention. Defaults to 'HK'
    for empty / unknown input so legacy code paths stay HK-flavoured."""
    if not ticker:
        return "HK"
    t = ticker.strip().upper()

    # Synthetic prefixes: market is embedded after the prefix.
    if t.startswith("&HK:"):
        return "HK"
    if t.startswith("&US:"):
        return "US"
    # Legacy `&NAME` (pre-namespacing) — treat as HK; the Phase 8 migration
    # rewrites these to `&HK:NAME` at the storage layer.
    if t.startswith("&"):
        return "HK"

    # Saved-portfolio synthetics: the helper can't tell, so default HK.
    # Callers that hold a `market` value from the portfolio row should pass
    # it explicitly rather than trust this branch.
    if t.startswith("@"):
        return "HK"

    # Indices.
    if t in _HK_INDICES:
        return "HK"
    if t in _US_INDICES:
        return "US"
    # Unknown ^TICKER — assume US (yfinance default).
    if t.startswith("^"):
        return "US"

    # Equity convention: `.HK` suffix = HK; anything else = US.
    if t.endswith(".HK"):
        return "HK"
    return "US"


def is_hk(ticker: str | None) -> bool:
    return market_of_ticker(ticker) == "HK"


def is_us(ticker: str | None) -> bool:
    return market_of_ticker(ticker) == "US"
