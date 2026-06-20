"""Centralized design system — colors, typography, shadows, reusable styles.

Single source of truth so a UI overhaul touches one file. All layout modules
import from here instead of hardcoding hex codes.

Palette inspired by modern fintech dashboards (FinVista-style):
  - Off-white / lavender background
  - White cards with subtle shadow + purple accent
  - Large bold numbers for key metrics
  - Purple primary, semantic green/red for direction
"""

# ============== Color tokens ==============

# Page / surface
BG              = "#f5f3fa"      # page background, very light lavender
CARD_BG         = "#ffffff"      # card surface
CARD_BG_SOFT    = "#fafaff"      # secondary card (nested)
PLOT_BG         = "#f8f7fc"      # plotly chart background
BORDER          = "#e8e6f1"      # card border, very light
BORDER_STRONG   = "#d7d3e8"      # focused / divider

# Text
TEXT            = "#1f1d2e"      # primary text
TEXT_MUTED      = "#6b6883"      # secondary text, labels
TEXT_FAINT      = "#a09dbb"      # very faint labels, footnotes
TEXT_ON_PRIMARY = "#ffffff"      # text on primary-colored buttons

# Brand
PRIMARY         = "#7e5cf0"      # purple primary
PRIMARY_HOVER   = "#6b48dc"
PRIMARY_SOFT    = "#ede8fd"      # tinted background for primary surfaces

# Semantic
SUCCESS         = "#16a34a"      # green
SUCCESS_SOFT    = "#dcfce7"
DANGER          = "#dc2626"      # red
DANGER_SOFT     = "#fee2e2"
WARNING         = "#f59e0b"      # amber
WARNING_SOFT    = "#fef3c7"
INFO            = "#0ea5e9"      # cyan-ish info
INFO_SOFT        = "#e0f2fe"

# Stock-price direction (CN/HK convention: red = up, green = down).
# PRICE_UP and PRICE_DOWN are the canonical names; UP and DOWN are kept as
# backward-compat aliases that now follow the same convention. Use these
# everywhere a colour encodes a price / return / sentiment direction.
# Use SUCCESS / DANGER directly only for non-price semantics: BUY/SELL
# action labels, save-success messages, SWOT strengths/threats, severity
# flags, screen-pass indicators.
PRICE_UP        = DANGER         # red — bullish, prices rising
PRICE_DOWN      = SUCCESS        # green — bearish, prices falling
UP              = PRICE_UP
DOWN            = PRICE_DOWN
MIXED           = WARNING
NEUTRAL         = "#94a3b8"      # slate

# Chart accents (multi-series)
ACCENT_1        = PRIMARY
ACCENT_2        = "#06b6d4"      # cyan
ACCENT_3        = "#f59e0b"      # amber
ACCENT_4        = "#ec4899"      # pink
ACCENT_5        = "#10b981"      # emerald


# ============== Typography sizes ==============

# Sizes in rem (relative to base 16px)
FONT_TINY       = "0.72rem"      # column headers, footnotes
FONT_SM         = "0.82rem"      # secondary body
FONT_BASE       = "0.95rem"      # default body
FONT_MD         = "1.1rem"       # subheads
FONT_LG         = "1.4rem"       # card titles
FONT_HERO_SM    = "1.6rem"       # stat numbers
FONT_HERO       = "2rem"         # primary hero numbers
FONT_HERO_LG    = "2.6rem"       # the biggest numbers (e.g. composite score)


# ============== Shadows ==============

SHADOW_SM       = "0 1px 3px rgba(126, 92, 240, 0.06), 0 1px 2px rgba(0, 0, 0, 0.04)"
SHADOW_MD       = "0 4px 12px rgba(126, 92, 240, 0.08), 0 2px 4px rgba(0, 0, 0, 0.04)"
SHADOW_LG       = "0 10px 30px rgba(126, 92, 240, 0.10), 0 4px 8px rgba(0, 0, 0, 0.04)"


# ============== Reusable style dicts ==============

# Card style for the most common container — white surface, subtle border + shadow
CARD_STYLE = {
    "background": CARD_BG,
    "border": f"1px solid {BORDER}",
    "borderRadius": "12px",
    "boxShadow": SHADOW_SM,
}

CARD_STYLE_SOFT = {
    "background": CARD_BG_SOFT,
    "border": f"1px solid {BORDER}",
    "borderRadius": "12px",
}

# Form inputs (text, dropdown, etc.)
INPUT_STYLE = {
    "background": CARD_BG,
    "color": TEXT,
    "border": f"1px solid {BORDER_STRONG}",
    "borderRadius": "8px",
}

INPUT_SUFFIX_STYLE = {
    "background": CARD_BG_SOFT,
    "color": TEXT_MUTED,
    "border": f"1px solid {BORDER_STRONG}",
}

# Big hero number block
HERO_NUMBER_STYLE = {
    "fontSize": FONT_HERO,
    "fontWeight": "700",
    "color": TEXT,
    "lineHeight": "1.1",
    "letterSpacing": "-0.02em",
}

HERO_NUMBER_LG_STYLE = {
    **HERO_NUMBER_STYLE,
    "fontSize": FONT_HERO_LG,
}


# ============== DataTable styles (Dash dash_table) ==============

DATATABLE_CELL = {
    "backgroundColor": CARD_BG,
    "color": TEXT,
    "fontSize": "0.85rem",
    "padding": "10px 12px",
    "fontFamily": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    "textAlign": "right",
    "border": f"1px solid {BORDER}",
}

DATATABLE_HEADER = {
    "backgroundColor": CARD_BG_SOFT,
    "color": TEXT_MUTED,
    "fontWeight": "600",
    "fontSize": "0.75rem",
    "textTransform": "uppercase",
    "letterSpacing": "0.05em",
    "border": f"1px solid {BORDER}",
}

DATATABLE_FILTER = {
    "backgroundColor": CARD_BG_SOFT,
    "color": TEXT,
}


# ============== Plotly layout helper ==============

def chart_layout(title: str = "", **overrides) -> dict:
    """Default Plotly layout for the light theme. Pass overrides as kwargs."""
    base = dict(
        title={"text": title, "font": {"color": TEXT, "size": 14, "family": "Inter, sans-serif"}},
        paper_bgcolor=CARD_BG,
        plot_bgcolor=PLOT_BG,
        font=dict(color=TEXT, size=12, family="Inter, sans-serif"),
        margin=dict(t=50, b=40, l=50, r=20),
        legend=dict(
            bgcolor="rgba(255,255,255,0.8)",
            bordercolor=BORDER,
            borderwidth=1,
            font=dict(color=TEXT_MUTED, size=11),
        ),
        xaxis=dict(gridcolor=BORDER, linecolor=BORDER, tickfont=dict(color=TEXT_MUTED)),
        yaxis=dict(gridcolor=BORDER, linecolor=BORDER, tickfont=dict(color=TEXT_MUTED)),
    )
    base.update(overrides)
    return base


# ============== Direction → color mapping (used by sentiment cards) ==============

DIRECTION_COLORS = {
    "UP": UP,
    "DOWN": DOWN,
    "MIXED": MIXED,
    "NEUTRAL": NEUTRAL,
}
