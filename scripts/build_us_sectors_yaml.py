"""Regenerate `config/us_sectors.yaml` from Wikipedia S&P 500 + Nasdaq-100.

Scrapes the GICS Sector + GICS Sub-Industry columns from Wikipedia and
maps each row through a hand-curated translation table into our 11
parent sectors + 75 sub-sectors. Writes one YAML row per ticker.

Run:
    python scripts/build_us_sectors_yaml.py

Output:
    config/us_sectors.yaml — ~500 rows covering S&P 500 + Nasdaq-100 union.
"""
from __future__ import annotations

import io
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "config" / "us_sectors.yaml"

# GICS Sector → our parent_sector (clean 1:1)
GICS_SECTOR = {
    "Communication Services": "Communication Services",
    "Consumer Discretionary":  "Consumer Cyclical",
    "Consumer Staples":        "Consumer Defensive",
    "Energy":                  "Energy",
    "Financials":              "Financial Services",
    "Health Care":             "Healthcare",
    "Industrials":             "Industrials",
    "Information Technology":  "Technology",
    "Materials":               "Basic Materials",
    "Real Estate":             "Real Estate",
    "Utilities":               "Utilities",
}

# GICS Sub-Industry -> (our_sub_sector, override_parent_sector_or_None).
# When the second element is set, it overrides the GICS Sector mapping —
# used for cross-parent moves like Homebuilding → Industrials.
GICS_SUB = {
    # Communication Services
    "Advertising":                            ("Advertising Agencies", None),
    "Broadcasting":                           ("Media & Entertainment", None),
    "Cable & Satellite":                      ("Media & Entertainment", None),
    "Integrated Telecommunication Services":  ("Telecom Services", None),
    "Interactive Home Entertainment":         ("Internet Content & Gaming", None),
    "Interactive Media & Services":           ("Internet Content & Gaming", None),
    "Movies & Entertainment":                 ("Media & Entertainment", None),
    "Publishing":                             ("Media & Entertainment", None),
    "Wireless Telecommunication Services":    ("Telecom Services", None),

    # Consumer Discretionary
    "Apparel Retail":                         ("Apparel & Specialty Retail", None),
    "Apparel, Accessories & Luxury Goods":    ("Luxury Goods", None),
    "Automobile Manufacturers":               ("Auto Manufacturers", None),
    "Automotive Parts & Equipment":           ("Auto Parts & Suppliers", None),
    "Automotive Retail":                      ("Auto Dealerships", None),
    "Broadline Retail":                       ("Apparel & Specialty Retail", None),
    "Casinos & Gaming":                       ("Gambling & Casinos", None),
    "Computer & Electronics Retail":          ("Apparel & Specialty Retail", None),
    "Consumer Electronics":                   ("Consumer Electronics & Devices", "Technology"),
    "Distributors":                           ("Business & Professional Services", "Industrials"),
    "Footwear":                               ("Textiles & Footwear", None),
    "Home Improvement Retail":                ("Home Furnishings & Appliances", None),
    "Homebuilding":                           ("Engineering & Construction", "Industrials"),
    "Homefurnishing Retail":                  ("Home Furnishings & Appliances", None),
    "Hotels, Resorts & Cruise Lines":         ("Travel & Hospitality", None),
    "Leisure Products":                       ("Travel & Hospitality", None),
    "Other Specialty Retail":                 ("Apparel & Specialty Retail", None),
    "Restaurants":                            ("Restaurants", None),
    "Specialized Consumer Services":          ("Personal Services", None),

    # Consumer Staples
    "Agricultural Products & Services":       ("Agricultural Inputs", "Basic Materials"),
    "Brewers":                                ("Beverages", None),
    "Consumer Staples Merchandise Retail":    ("Food Production, Distribution & Retail", None),
    "Distillers & Vintners":                  ("Beverages", None),
    "Food Distributors":                      ("Food Production, Distribution & Retail", None),
    "Food Retail":                            ("Food Production, Distribution & Retail", None),
    "Household Products":                     ("Household & Personal Products", None),
    "Packaged Foods & Meats":                 ("Packaged Foods", None),
    "Personal Care Products":                 ("Household & Personal Products", None),
    "Soft Drinks & Non-alcoholic Beverages":  ("Beverages", None),
    "Tobacco":                                ("Packaged Foods", None),

    # Energy (all 5 -> Oil & Gas)
    "Integrated Oil & Gas":                   ("Oil & Gas", None),
    "Oil & Gas Equipment & Services":         ("Oil & Gas", None),
    "Oil & Gas Exploration & Production":     ("Oil & Gas", None),
    "Oil & Gas Refining & Marketing":         ("Oil & Gas", None),
    "Oil & Gas Storage & Transportation":     ("Oil & Gas", None),

    # Financials
    "Asset Management & Custody Banks":       ("Asset Management", None),
    "Consumer Finance":                       ("Credit Services", None),
    "Diversified Banks":                      ("Banks", None),
    "Financial Exchanges & Data":             ("Capital Markets", None),
    "Insurance Brokers":                      ("Insurance", None),
    "Investment Banking & Brokerage":         ("Capital Markets", None),
    "Life & Health Insurance":                ("Insurance", None),
    "Multi-Sector Holdings":                  ("Financial Conglomerates", None),
    "Multi-line Insurance":                   ("Insurance", None),
    "Property & Casualty Insurance":          ("Insurance", None),
    "Regional Banks":                         ("Banks", None),
    "Reinsurance":                            ("Insurance", None),
    "Transaction & Payment Processing Services": ("Credit Services", None),

    # Health Care
    "Biotechnology":                          ("Biotechnology", None),
    "Health Care Distributors":               ("Pharmacy Retail & Distribution", None),
    "Health Care Equipment":                  ("Medical Devices & Instruments", None),
    "Health Care Facilities":                 ("Medical Care Facilities", None),
    "Health Care Services":                   ("Medical Care Facilities", None),
    "Health Care Supplies":                   ("Medical Devices & Instruments", None),
    "Health Care Technology":                 ("Health Information Services", None),
    "Life Sciences Tools & Services":         ("Diagnostics & Research", None),
    "Managed Health Care":                    ("Insurance", "Financial Services"),
    "Pharmaceuticals":                        ("Drug Manufacturing", None),

    # Industrials
    "Aerospace & Defense":                    ("Aerospace & Defense", None),
    "Agricultural & Farm Machinery":          ("Industrial Machinery", None),
    "Air Freight & Logistics":                ("Logistics & Freight", None),
    "Building Products":                      ("Building Products & Equipment", None),
    "Cargo Ground Transportation":            ("Logistics & Freight", None),
    "Construction & Engineering":             ("Engineering & Construction", None),
    "Construction Machinery & Heavy Transportation Equipment": ("Industrial Machinery", None),
    "Data Processing & Outsourced Services":  ("Business & Professional Services", None),
    "Diversified Support Services":           ("Business & Professional Services", None),
    "Electrical Components & Equipment":      ("Electrical Equipment", None),
    "Environmental & Facilities Services":    ("Environmental Services", None),
    "Heavy Electrical Equipment":             ("Electrical Equipment", None),
    "Human Resource & Employment Services":   ("Business & Professional Services", None),
    "Industrial Conglomerates":               ("Conglomerates", None),
    "Industrial Machinery & Supplies & Components": ("Industrial Machinery", None),
    "Passenger Airlines":                     ("Passenger & Air Transport", None),
    "Passenger Ground Transportation":        ("Passenger & Air Transport", None),
    "Rail Transportation":                    ("Passenger & Air Transport", None),
    "Research & Consulting Services":         ("Business & Professional Services", None),
    "Trading Companies & Distributors":       ("Business & Professional Services", None),

    # Information Technology
    "Application Software":                   ("Application Software", None),
    "Communications Equipment":               ("Telecom Equipment", None),
    "Electronic Components":                  ("Tech Components & Distribution", None),
    "Electronic Equipment & Instruments":     ("Tech Components & Distribution", None),
    "Electronic Manufacturing Services":      ("Tech Components & Distribution", None),
    "IT Consulting & Other Services":         ("IT Services & Consulting", None),
    "Internet Services & Infrastructure":     ("Platforms & Cloud Infrastructure", None),
    "Semiconductor Materials & Equipment":    ("Semiconductors & Equipment", None),
    "Semiconductors":                         ("Semiconductors & Equipment", None),
    "Systems Software":                       ("Platforms & Cloud Infrastructure", None),
    "Technology Distributors":                ("Tech Components & Distribution", None),
    "Technology Hardware, Storage & Peripherals": ("Consumer Electronics & Devices", None),

    # Materials
    "Commodity Chemicals":                    ("Chemicals", None),
    "Construction Materials":                 ("Building Materials & Cement", None),
    "Copper":                                 ("Base Metals & Mining", None),
    "Fertilizers & Agricultural Chemicals":   ("Agricultural Inputs", None),
    "Gold":                                   ("Precious Metals & Mining", None),
    "Industrial Gases":                       ("Chemicals", None),
    "Metal, Glass & Plastic Containers":      ("Packaging & Containers", "Consumer Cyclical"),
    "Paper & Plastic Packaging Products & Materials": ("Packaging & Containers", "Consumer Cyclical"),
    "Specialty Chemicals":                    ("Chemicals", None),
    "Steel":                                  ("Steel", None),

    # Real Estate — REIT subtypes all collapse to Diversified Real Estate
    "Data Center REITs":                      ("Diversified Real Estate", None),
    "Health Care REITs":                      ("Diversified Real Estate", None),
    "Hotel & Resort REITs":                   ("Diversified Real Estate", None),
    "Industrial REITs":                       ("Diversified Real Estate", None),
    "Multi-Family Residential REITs":         ("Diversified Real Estate", None),
    "Office REITs":                           ("Diversified Real Estate", None),
    "Other Specialized REITs":                ("Diversified Real Estate", None),
    "Real Estate Services":                   ("Property Management & Services", None),
    "Retail REITs":                           ("Diversified Real Estate", None),
    "Self-Storage REITs":                     ("Diversified Real Estate", None),
    "Single-Family Residential REITs":        ("Diversified Real Estate", None),
    "Telecom Tower REITs":                    ("Diversified Real Estate", None),
    "Timber REITs":                           ("Diversified Real Estate", None),

    # Utilities
    "Electric Utilities":                              ("Regulated Electric Utilities", None),
    "Gas Utilities":                                   ("Regulated Gas Utilities", None),
    "Independent Power Producers & Energy Traders":    ("Independent Power Producers", None),
    "Multi-Utilities":                                 ("Regulated Electric Utilities", None),
    "Water Utilities":                                 ("Regulated Water Utilities", None),
}

HEADERS = {"User-Agent": "Mozilla/5.0"}


def _norm_ticker(s):
    return (s or "").strip().upper().replace(" ", "-").replace(".", "-")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    entries = {}  # ticker -> (sub_sector, parent_sector, source)
    unmapped = Counter()

    # S&P 500
    print("Fetching S&P 500 ...")
    sp = pd.read_html(io.StringIO(requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers=HEADERS, timeout=30).text))[0]
    for _, row in sp.iterrows():
        t = _norm_ticker(row.get("Symbol"))
        sec = row.get("GICS Sector")
        sub = row.get("GICS Sub-Industry")
        if not t or sec not in GICS_SECTOR:
            continue
        if sub not in GICS_SUB:
            unmapped[sub] += 1
            continue
        our_sub, override_parent = GICS_SUB[sub]
        parent = override_parent or GICS_SECTOR[sec]
        entries[t] = (our_sub, parent, "S&P 500")
    print(f"  S&P 500: {len(entries)} mapped")

    # Nasdaq-100
    print("Fetching Nasdaq-100 ...")
    try:
        nq_tables = pd.read_html(io.StringIO(requests.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            headers=HEADERS, timeout=30).text))
        added = 0
        for tbl in nq_tables:
            cols = {str(c).strip() for c in tbl.columns}
            if "Ticker" in cols and "GICS Sub-Industry" in cols:
                for _, row in tbl.iterrows():
                    t = _norm_ticker(row.get("Ticker"))
                    sec = row.get("GICS Sector")
                    sub = row.get("GICS Sub-Industry")
                    if not t or t in entries or sec not in GICS_SECTOR:
                        continue
                    if sub not in GICS_SUB:
                        unmapped[sub] += 1
                        continue
                    our_sub, override_parent = GICS_SUB[sub]
                    parent = override_parent or GICS_SECTOR[sec]
                    entries[t] = (our_sub, parent, "Nasdaq-100")
                    added += 1
                break
        print(f"  Nasdaq-100: +{added} unique")
    except Exception as e:
        print(f"  Nasdaq-100 fetch failed: {e}", file=sys.stderr)

    if unmapped:
        print(f"\nWARN: {sum(unmapped.values())} rows had unmapped GICS sub-industries:")
        for sub, n in unmapped.most_common(20):
            print(f"  [{n:>3}] {sub}")

    # Distribution sanity
    counts = Counter(v[0] for v in entries.values())
    print(f"\nSub-sector distribution (top 15):")
    for sub, n in counts.most_common(15):
        print(f"  [{n:>3}] {sub}")

    # Emit YAML
    lines = [
        "# US ticker -> {parent_sector, sub_sector} overrides.",
        "# Source: Wikipedia S&P 500 + Nasdaq-100 GICS taxonomy, mapped to",
        "# our 11 parent sectors + 75 sub-sectors via the translation table",
        "# in scripts/build_us_sectors_yaml.py. Re-generate by running that",
        "# script. Tweak individual ticker promotions by hand if needed —",
        "# subsequent re-runs will preserve your edits only if you update",
        "# the script's GICS_SUB map; otherwise the run overwrites this file.",
        "#",
        "# Loaded by universe/reconciler.py:_load_us_sectors_yaml() and",
        "# applied during `python main.py universe-us seed`.",
        f"# Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} - {len(entries)} tickers",
        "",
        "overrides:",
    ]
    for t in sorted(entries):
        sub, parent, src = entries[t]
        sub_q = f"'{sub}'" if any(c in sub for c in [":", "&", ","]) else sub
        parent_q = f"'{parent}'" if any(c in parent for c in [":", "&"]) else parent
        lines.append(f"  {t}: {{ parent_sector: {parent_q}, sub_sector: {sub_q} }}  # {src}")
    lines.append("")

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT_PATH} ({len(entries)} entries)")


if __name__ == "__main__":
    main()
