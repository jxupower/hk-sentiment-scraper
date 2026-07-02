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
    # Note: Managed Health Care (UNH/ELV/HUM/CI/CVS-style) was previously
    # routed to Insurance / Financial Services — wrong parent. Now lives in
    # its own Healthcare Plans & Managed Care bucket under Healthcare.
    # Medical Devices and Medical Instruments & Supplies are now split back
    # into two distinct buckets (was merged into Medical Devices & Instruments).
    "Biotechnology":                          ("Biotechnology", None),
    "Health Care Distributors":               ("Pharmacy Retail & Distribution", None),
    "Health Care Equipment":                  ("Medical Devices", None),
    "Health Care Facilities":                 ("Medical Care Facilities", None),
    "Health Care Services":                   ("Medical Care Facilities", None),
    "Health Care Supplies":                   ("Medical Instruments & Supplies", None),
    "Health Care Technology":                 ("Health Information Services", None),
    "Life Sciences Tools & Services":         ("Diagnostics & Research", None),
    "Managed Health Care":                    ("Healthcare Plans & Managed Care", None),
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

    # Real Estate — REIT subtypes split into 7 fine-grained buckets matching
    # the yfinance `REIT - *` industries downstream of config/sub_sectors.yaml.
    # Data Center / Self-Storage / Telecom Tower / Timber don't map cleanly to
    # any of the 6 specialty buckets, so they fold into Diversified & Specialty.
    "Data Center REITs":                      ("Diversified & Specialty REITs", None),
    "Health Care REITs":                      ("Healthcare REITs", None),
    "Hotel & Resort REITs":                   ("Diversified & Specialty REITs", None),
    "Industrial REITs":                       ("Industrial REITs", None),
    "Multi-Family Residential REITs":         ("Residential REITs", None),
    "Office REITs":                           ("Office REITs", None),
    "Other Specialized REITs":                ("Diversified & Specialty REITs", None),
    "Real Estate Services":                   ("Property Management & Services", None),
    "Retail REITs":                           ("Retail REITs", None),
    "Self-Storage REITs":                     ("Diversified & Specialty REITs", None),
    "Single-Family Residential REITs":        ("Residential REITs", None),
    "Telecom Tower REITs":                    ("Diversified & Specialty REITs", None),
    "Timber REITs":                           ("Diversified & Specialty REITs", None),

    # Utilities
    "Electric Utilities":                              ("Regulated Electric Utilities", None),
    "Gas Utilities":                                   ("Regulated Gas Utilities", None),
    "Independent Power Producers & Energy Traders":    ("Independent Power Producers", None),
    "Multi-Utilities":                                 ("Regulated Electric Utilities", None),
    "Water Utilities":                                 ("Regulated Water Utilities", None),
}

# Per-ticker corrections applied AFTER GICS_SUB mapping. Use when GICS
# lumps economically distinct names under one sub-industry (e.g.
# `Technology Hardware, Storage & Peripherals` covers both consumer
# devices like AAPL and enterprise infrastructure like HPE/NTAP/SMCI).
# These overrides win in us_sectors.yaml; downstream Tier-2
# ticker_overrides in config/sub_sectors.yaml can still override these.
TICKER_OVERRIDES: dict[str, tuple[str, str | None]] = {
    # Enterprise IT hardware/storage — wrong peer pool in "Consumer
    # Electronics & Devices" (AAPL/DELL/HPQ live there).
    "HPE":   ("Tech Components & Distribution", "Technology"),
    "NTAP":  ("Tech Components & Distribution", "Technology"),
    "SMCI":  ("Tech Components & Distribution", "Technology"),

    # Defense-focused IT services — Leidos is ~70% defense/intel.
    "LDOS":  ("Aerospace & Defense", "Industrials"),

    # Electrical equipment maker — GICS "Industrial Machinery & Supplies
    # & Components" is too broad; bucket exists for electrical names.
    "HUBB":  ("Electrical Equipment", "Industrials"),

    # MRO/industrial distributor — not a machinery maker.
    "GWW":   ("Business & Professional Services", "Industrials"),

    # Diversified industrial holding company — owns measurement, software,
    # healthcare imaging, etc. Doesn't fit hardware-components bucket.
    "ROP":   ("Conglomerates", "Industrials"),

    # Auto parts distributor — fits the auto-cycle peer set.
    "GPC":   ("Auto Parts & Suppliers", "Consumer Cyclical"),

    # ---- Severity-3 refinements (boundary calls) -------------------------
    # Payroll / HR SaaS — primary business is software not services.
    "ADP":   ("Application Software", "Technology"),
    "PAYX":  ("Application Software", "Technology"),
    # Broadridge — fintech infrastructure SaaS (proxy voting, settlement).
    "BR":    ("Application Software", "Technology"),

    # Data analytics for financial markets — peer with MSCI/SPGI/MCO.
    "EFX":   ("Capital Markets", "Financial Services"),
    "VRSK":  ("Capital Markets", "Financial Services"),

    # Pure cable operator (no content arm like CMCSA's NBCU) — telecom-shaped.
    "CHTR":  ("Telecom Services", "Communication Services"),

    # Principal — retirement plans business is closer to asset mgmt than
    # life insurance. Size split downstream will land it in Large/Mid AM.
    "PFG":   ("Asset Management", "Financial Services"),

    # Applovin — mobile ad-tech platform (yfinance correctly: Advertising).
    "APP":   ("Advertising Agencies", "Communication Services"),
    # Palantir — enterprise data platform; fits cloud-infra peers.
    "PLTR":  ("Platforms & Cloud Infrastructure", "Technology"),

    # Weyerhaeuser — timberland operator structured as a REIT but
    # economically a forest-products name (correlates with lumber prices,
    # not real estate cap rates).
    "WY":    ("Forest Products & Paper", "Basic Materials"),
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
        # Per-ticker overrides win over the GICS class mapping.
        if t in TICKER_OVERRIDES:
            our_sub, override_parent = TICKER_OVERRIDES[t]
            parent = override_parent or parent
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
                    if t in TICKER_OVERRIDES:
                        our_sub, override_parent = TICKER_OVERRIDES[t]
                        parent = override_parent or parent
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
