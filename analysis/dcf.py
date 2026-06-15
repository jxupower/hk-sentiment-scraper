"""Two-stage Discounted Cash Flow (DCF) intrinsic-value calculator.

Plain Bagel's step 5 includes absolute valuation via DCF — projecting free cash
flow into the future and discounting back to present. We implement a standard
two-stage DCF:
  - Stage 1: explicit 5-year growth at growth_y1_5
  - Stage 2: explicit years 6-10 at growth_y6_10 (transitional)
  - Stage 3: terminal value via Gordon Growth at terminal_growth (perpetual)

The intrinsic value per share is then compared to the current price to compute
margin of safety.

HONEST LIMITATION — FCF proxy:
  akshare/yfinance don't reliably expose free cash flow history for HK tickers.
  We use `eps_ttm × shares_outstanding × cf_conversion_factor` as the FCF proxy.
  `cf_conversion_factor` defaults to 0.8 — i.e. assume 80% of net income converts
  to FCF on average. The user can adjust this slider per ticker (e.g. capex-heavy
  industries like Utilities convert less; software more). When the company
  reports actual FCF reliably (rare for HK), the user should override the base_fcf
  directly in the sliders.

Sensitivity tables let you see how the intrinsic value moves as you vary any 2
of the 4 key inputs (growth, terminal_growth, WACC, conversion_factor).

Per-year breakdown (added 2026-06): compute_dcf() now also emits a list of
DCFYearBreakdown rows so the Section 5 walkthrough card in the dashboard can
display the math step by step (year, growth_used, fcf, discount_factor, pv)
without re-running the projection.

Growth default provenance (added 2026-06): default_inputs_from_snapshot() now
walks a 3-tier resolution chain (median of 5y CAGRs → analyst forward consensus
→ trailing earnings_growth → hardcoded floor) and returns a GrowthProvenance
alongside the inputs so the UI can explain why the slider sits where it does.
"""
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class DCFInputs:
    base_fcf: float                 # most recent annual FCF (or proxy: net_income × cf_conversion)
    growth_y1_5: float = 0.10       # average annual FCF growth for years 1-5 (fraction)
    growth_y6_10: float = 0.05      # transitional growth years 6-10
    terminal_growth: float = 0.025  # perpetual growth beyond year 10 (typically <= long-run GDP)
    wacc: float = 0.09              # weighted-average cost of capital (discount rate)
    shares_outstanding: float = 0   # for per-share intrinsic value
    current_price: float = 0        # for margin-of-safety calc

    def validate(self) -> Optional[str]:
        """Returns an error message string if inputs are invalid, else None."""
        if self.shares_outstanding is None or self.shares_outstanding <= 0:
            return "shares_outstanding must be positive"
        if self.wacc is None or self.wacc <= 0:
            return "WACC must be positive"
        if self.terminal_growth >= self.wacc:
            return f"terminal_growth ({self.terminal_growth:.2%}) must be less than WACC ({self.wacc:.2%})"
        if self.base_fcf is None or not math.isfinite(self.base_fcf):
            return "base_fcf is invalid"
        return None


@dataclass
class DCFYearBreakdown:
    """One row in the per-year DCF projection table — used by the Section 5
    'DCF walkthrough' card so users can audit every step from base FCF through
    terminal-value PV. Populated by compute_dcf() in lockstep with the math.

    `year` is 1..10 for the explicit projection; the terminal row uses year=10
    with `is_terminal=True` (PV of the Gordon-growth perpetuity discounted at
    end of year 10)."""
    year: int
    growth_used: float           # the growth rate that drove this year's FCF
    fcf: float                   # projected free cash flow for this year
    discount_factor: float       # 1 / (1 + wacc) ** year
    pv: float                    # fcf × discount_factor (or PV of terminal at year 10)
    is_terminal: bool = False


@dataclass
class GrowthProvenance:
    """Records which tier of the growth-default resolver provided the Y1-5
    slider seed and what candidates were available, so the dashboard can show
    a 'Default 11.2% — median of 5y CAGRs (rev 11.2%, eps 14.1%, bps 8.7%)'
    subtitle directly under the slider."""
    winning_tier: str            # "cagr_median" | "analyst_consensus" | "trailing" | "hardcoded"
    chosen_value: float          # the value passed into DCFInputs.growth_y1_5 (post-clamp)
    raw_value: Optional[float]   # winning value pre-clamp (None if hardcoded)
    median_inputs: dict          # {"earnings_5y_cagr": 0.14, ...} — Nones included
    analyst_5y_consensus: Optional[float]
    trailing_earnings_growth: Optional[float]
    hardcoded_floor: float
    clamped: bool                # True if raw_value was clipped to slider bounds


@dataclass
class DCFResult:
    projected_fcf: list[float]       # year-by-year FCF for years 1-10
    discounted_fcf: list[float]      # PV of each year's FCF
    terminal_value: float            # nominal terminal value at year 10
    discounted_terminal: float       # PV of terminal value
    enterprise_value: float          # sum of discounted FCFs + discounted terminal
    intrinsic_value_per_share: float
    current_price: float
    margin_of_safety: Optional[float]  # (intrinsic - current) / intrinsic; >0 = undervalued
    error: Optional[str] = None
    # Per-year breakdown rows — 10 explicit years + 1 terminal row. Empty when
    # the input validation failed.
    breakdown: list[DCFYearBreakdown] = field(default_factory=list)


def compute_dcf(inputs: DCFInputs) -> DCFResult:
    err = inputs.validate()
    if err:
        return DCFResult(
            projected_fcf=[], discounted_fcf=[], terminal_value=0.0,
            discounted_terminal=0.0, enterprise_value=0.0,
            intrinsic_value_per_share=0.0, current_price=inputs.current_price or 0.0,
            margin_of_safety=None, error=err, breakdown=[],
        )

    # Stage 1 + 2: years 1-10. We thread the breakdown rows alongside so the
    # dashboard's walkthrough card has every intermediate value without
    # re-projecting.
    projected: list[float] = []
    discounted: list[float] = []
    breakdown: list[DCFYearBreakdown] = []

    cur = inputs.base_fcf
    for year in range(1, 11):
        growth = inputs.growth_y1_5 if year <= 5 else inputs.growth_y6_10
        cur *= (1 + growth)
        projected.append(cur)
        df = 1.0 / ((1 + inputs.wacc) ** year)
        pv = cur * df
        discounted.append(pv)
        breakdown.append(DCFYearBreakdown(
            year=year, growth_used=growth, fcf=cur,
            discount_factor=df, pv=pv, is_terminal=False,
        ))

    # Terminal value at end of year 10 via Gordon Growth. The "year 11" FCF
    # is virtual — we don't grow further explicitly, the perpetuity captures
    # the rest. PV is discounted at year 10 (the terminal is received at the
    # boundary of year 10/11).
    fcf_year_11 = projected[-1] * (1 + inputs.terminal_growth)
    terminal = fcf_year_11 / (inputs.wacc - inputs.terminal_growth)
    df_10 = 1.0 / ((1 + inputs.wacc) ** 10)
    discounted_terminal = terminal * df_10
    breakdown.append(DCFYearBreakdown(
        year=10, growth_used=inputs.terminal_growth,
        fcf=terminal, discount_factor=df_10, pv=discounted_terminal,
        is_terminal=True,
    ))

    enterprise_value = sum(discounted) + discounted_terminal
    intrinsic_per_share = enterprise_value / inputs.shares_outstanding

    margin_of_safety = None
    if inputs.current_price and inputs.current_price > 0:
        margin_of_safety = (intrinsic_per_share - inputs.current_price) / intrinsic_per_share

    return DCFResult(
        projected_fcf=projected, discounted_fcf=discounted,
        terminal_value=terminal, discounted_terminal=discounted_terminal,
        enterprise_value=enterprise_value,
        intrinsic_value_per_share=intrinsic_per_share,
        current_price=inputs.current_price,
        margin_of_safety=margin_of_safety,
        breakdown=breakdown,
    )


def sensitivity_table(base_inputs: DCFInputs, vary_x: str, vary_y: str,
                      x_values: list[float], y_values: list[float],
                      ) -> pd.DataFrame:
    """Compute intrinsic-value-per-share grid by varying two DCFInputs fields.
    Useful for visualizing how robust the valuation is to assumption changes.

    Returns DataFrame indexed by y_values, columns x_values, cells = intrinsic
    value per share."""
    rows = []
    for y_val in y_values:
        row = []
        for x_val in x_values:
            # Build inputs with the two overrides
            kwargs = base_inputs.__dict__.copy()
            kwargs[vary_x] = x_val
            kwargs[vary_y] = y_val
            res = compute_dcf(DCFInputs(**kwargs))
            row.append(res.intrinsic_value_per_share if not res.error else None)
        rows.append(row)
    return pd.DataFrame(rows, index=[f"{y:.3f}" for y in y_values],
                         columns=[f"{x:.3f}" for x in x_values])


# Slider bounds — keep in sync with stock_research_layout.py's sr-dcf-g15.
_Y15_SLIDER_LO = -0.10
_Y15_SLIDER_HI = 0.30
_HARDCODED_GROWTH = 0.08


def default_inputs_from_snapshot(
    snapshot: dict,
    cf_conversion_factor: float = 0.8,
    *,
    cagr_earnings_5y: Optional[float] = None,
    cagr_revenue_5y: Optional[float] = None,
    cagr_bps_5y: Optional[float] = None,
    analyst_growth_5y: Optional[float] = None,
) -> Optional[tuple[DCFInputs, GrowthProvenance]]:
    """Build sensible default DCFInputs from a fundamentals_snapshots row and
    return them alongside a GrowthProvenance describing how Y1-5 was picked.

    Resolution chain for the Y1-5 growth rate (most consequential slider):
      1. **Median of 5-year CAGRs** across earnings, revenue, book value per
         share — pulled from `cagr_earnings/revenue/bps[5]` in the orchestrator.
         Median of however many are non-None. Most robust against single-metric
         noise.
      2. **Forward analyst consensus** — `Ticker.growth_estimates['+5y']` from
         yfinance, fetched on demand and cached locally for 7 days. Often empty
         for HK names, hence the fallback.
      3. **Trailing earnings_growth** — the snapshot's own yfinance YoY field.
         Existing behaviour; kept for parity when CAGR + consensus are both
         missing.
      4. **Hardcoded 8% floor** — last resort when no signal exists.

    Returns None if eps_ttm/shares are missing (no base_fcf possible)."""
    eps = snapshot.get("eps_ttm")
    shares = snapshot.get("shares_outstanding")
    trailing_growth = snapshot.get("earnings_growth")
    price = snapshot.get("last_price")

    if eps is None or shares is None or eps <= 0 or shares <= 0:
        return None

    net_income = float(eps) * float(shares)
    base_fcf = net_income * cf_conversion_factor

    # Walk the 3-tier resolver. Each branch records the winning tier and the
    # raw (pre-clamp) value for the provenance object.
    cagr_inputs = {
        "earnings_5y_cagr": cagr_earnings_5y,
        "revenue_5y_cagr": cagr_revenue_5y,
        "bps_5y_cagr": cagr_bps_5y,
    }
    available_cagrs = [v for v in cagr_inputs.values() if v is not None]
    raw_value: Optional[float] = None
    if available_cagrs:
        raw_value = statistics.median(available_cagrs)
        tier = "cagr_median"
    elif (analyst_growth_5y is not None
            and -0.5 < float(analyst_growth_5y) < 0.5):
        raw_value = float(analyst_growth_5y)
        tier = "analyst_consensus"
    elif (trailing_growth is not None
            and -0.30 <= float(trailing_growth) <= 0.50):
        raw_value = float(trailing_growth)
        tier = "trailing"
    else:
        raw_value = None
        tier = "hardcoded"

    pre_clamp = raw_value if raw_value is not None else _HARDCODED_GROWTH
    clamped_g1 = max(_Y15_SLIDER_LO, min(_Y15_SLIDER_HI, pre_clamp))
    clamped = (pre_clamp != clamped_g1)
    g2 = max(0.03, clamped_g1 / 2)    # fade toward GDP

    inputs = DCFInputs(
        base_fcf=base_fcf,
        growth_y1_5=clamped_g1,
        growth_y6_10=g2,
        terminal_growth=0.025,
        wacc=0.09,
        shares_outstanding=float(shares),
        current_price=float(price) if price else 0.0,
    )
    prov = GrowthProvenance(
        winning_tier=tier,
        chosen_value=clamped_g1,
        raw_value=raw_value,
        median_inputs=cagr_inputs,
        analyst_5y_consensus=(float(analyst_growth_5y)
                                if analyst_growth_5y is not None else None),
        trailing_earnings_growth=(float(trailing_growth)
                                    if trailing_growth is not None else None),
        hardcoded_floor=_HARDCODED_GROWTH,
        clamped=clamped,
    )
    return inputs, prov
