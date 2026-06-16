"""Famous-investor composite V/Q/G screens, exposed on the Screener tab as
one-click preset buttons.

Each preset is a dict of slider overrides keyed by the NUMERIC_FILTERS slug
(see screener_layout.py). Slider units must match the user-facing axis of
that filter, which is:

    pe       absolute trailing P/E
    fwdpe    absolute forward P/E
    pb       absolute P/B
    evebitda absolute EV/EBITDA
    divyield dividend yield in percent (e.g. 4.0 = 4%)
    roe      ROE in percent  (raw row value × 100)
    egrowth  earnings growth in percent (raw row value × 100)
    de       debt/equity in percent (e.g. 50 = 50%)
    beta     raw beta
    mcap     market cap in HK$ billions

Slugs absent from a preset's `sliders` dict are left at their full-range
default, so they don't accidentally filter anything out. Each preset is a
faithful-as-possible mapping of the named framework to the fields we actually
have in fundamentals_snapshots — small approximations are called out in the
description (e.g. Lynch GARP can't enforce growth >= P/E/2 as a single
range filter, so it leans on P/E cap + a generous growth floor).
"""

INVESTOR_PRESETS = [
    {
        "id": "buffett",
        "label": "Buffett",
        "title": "Wonderful Co. at a Fair Price",
        "description": (
            "ROE ≥ 15%, D/E ≤ 50%, large-cap (≥ HK$10B), "
            "P/E in [10, 25]. Quality with a sane valuation."
        ),
        "sliders": {
            "pe":   [10, 25],
            "roe":  [15, 100],
            "de":   [0, 50],
            "mcap": [10, 3000],
        },
    },
    {
        "id": "graham",
        "label": "Graham",
        "title": "Defensive Value",
        "description": (
            "P/E ≤ 15, P/B ≤ 1.5, pays a dividend, ≥ HK$5B. "
            "Margin-of-safety classic."
        ),
        "sliders": {
            "pe":       [0, 15],
            "pb":       [0, 1.5],
            "divyield": [0.01, 20],
            "mcap":     [5, 3000],
        },
    },
    {
        "id": "lynch",
        "label": "Lynch GARP",
        "title": "Growth at a Reasonable Price",
        "description": (
            "P/E ≤ 20, earnings growth ≥ 15%, ROE ≥ 12%, ≥ HK$2B. "
            "Approximates PEG ≤ ~1 via the P/E cap + growth floor."
        ),
        "sliders": {
            "pe":      [0, 20],
            "egrowth": [15, 500],
            "roe":     [12, 100],
            "mcap":    [2, 3000],
        },
    },
    {
        "id": "greenblatt",
        "label": "Magic Formula",
        "title": "Greenblatt Magic Formula",
        "description": (
            "EV/EBITDA ≤ 10, ROE ≥ 20%, ≥ HK$2B. Combines earnings "
            "yield + return on capital — sort the results by ROE."
        ),
        "sliders": {
            "evebitda": [0, 10],
            "roe":      [20, 100],
            "mcap":     [2, 3000],
        },
    },
    {
        "id": "druckenmiller",
        "label": "Druckenmiller",
        "title": "Growth Momentum",
        "description": (
            "Earnings growth ≥ 25%, ROE ≥ 15%, ≥ HK$5B. "
            "Earnings acceleration — no valuation cap on purpose."
        ),
        "sliders": {
            "egrowth": [25, 500],
            "roe":     [15, 100],
            "mcap":    [5, 3000],
        },
    },
]
