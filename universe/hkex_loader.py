from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from utils.logger import get_logger

logger = get_logger(__name__)

HKEX_LIST_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
SHEET_NAME = "ListOfSecurities"
HEADER_ROW = 2  # rows 0-1 are title + "Updated as at ..."; row 2 is the column header


def _to_yfinance_ticker(code: int) -> str:
    """HKEX integer code → yfinance format. Pads to min 4 digits; preserves longer codes verbatim."""
    return f"{int(code):04d}.HK"


def _parse_lot_size(value) -> Optional[int]:
    """Board Lot is a string like '1,000' or '500'. Some rows may be NaN."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def download(cache_dir: Path) -> Path:
    """Download the HKEX securities list, save to cache_dir/hkex_YYYYMMDD.xlsx, return path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"hkex_{datetime.utcnow().strftime('%Y%m%d')}.xlsx"
    logger.info("Downloading HKEX securities list from %s", HKEX_LIST_URL)
    response = requests.get(HKEX_LIST_URL, timeout=30)
    response.raise_for_status()
    cache_path.write_bytes(response.content)
    logger.info("Saved HKEX list to %s (%d bytes)", cache_path, len(response.content))
    return cache_path


def parse(xlsx_path: Path) -> list[dict]:
    """Parse the HKEX list Excel file, filter to equities, return normalized records.

    Each record: {ticker, hkex_code, name, listing_category, lot_size}.
    """
    df = pd.read_excel(xlsx_path, sheet_name=SHEET_NAME, header=HEADER_ROW)
    equities = df[df["Category"] == "Equity"].copy()
    logger.info("HKEX file contains %d total rows, %d equities", len(df), len(equities))

    records = []
    for row in equities.to_dict("records"):
        code = row.get("Stock Code")
        name = row.get("Name of Securities")
        sub_category = row.get("Sub-Category")
        board_lot = row.get("Board Lot")
        if code is None or pd.isna(code) or name is None or pd.isna(name):
            continue
        records.append({
            "ticker": _to_yfinance_ticker(code),
            "hkex_code": f"{int(code):04d}",
            "name": str(name).strip(),
            "listing_category": str(sub_category).strip() if sub_category and not pd.isna(sub_category) else "Equity",
            "lot_size": _parse_lot_size(board_lot),
        })
    return records


def download_and_parse(cache_dir: Path) -> list[dict]:
    """Convenience wrapper: download fresh, then parse."""
    return parse(download(cache_dir))
