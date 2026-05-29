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
"""
import math
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


def compute_dcf(inputs: DCFInputs) -> DCFResult:
    err = inputs.validate()
    if err:
        return DCFResult(
            projected_fcf=[], discounted_fcf=[], terminal_value=0.0,
            discounted_terminal=0.0, enterprise_value=0.0,
            intrinsic_value_per_share=0.0, current_price=inputs.current_price or 0.0,
            margin_of_safety=None, error=err,
        )

    # Stage 1: years 1-5
    projected: list[float] = []
    cur = inputs.base_fcf
    for _ in range(5):
        cur *= (1 + inputs.growth_y1_5)
        projected.append(cur)
    # Stage 2: years 6-10
    for _ in range(5):
        cur *= (1 + inputs.growth_y6_10)
        projected.append(cur)

    # Discount each year's FCF to present
    discounted: list[float] = []
    for year, fcf in enumerate(projected, start=1):
        discounted.append(fcf / ((1 + inputs.wacc) ** year))

    # Terminal value at end of year 10 via Gordon Growth
    fcf_year_11 = projected[-1] * (1 + inputs.terminal_growth)
    terminal = fcf_year_11 / (inputs.wacc - inputs.terminal_growth)
    discounted_terminal = terminal / ((1 + inputs.wacc) ** 10)

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


def default_inputs_from_snapshot(snapshot: dict,
                                  cf_conversion_factor: float = 0.8) -> Optional[DCFInputs]:
    """Build sensible default DCFInputs from a fundamentals_snapshots row.

    The base_fcf proxy = eps_ttm × shares_outstanding × cf_conversion_factor.
    Growth defaults: use the snapshot's earnings_growth for y1-5, half of that
    for y6-10 (fade), 2.5% terminal."""
    eps = snapshot.get("eps_ttm")
    shares = snapshot.get("shares_outstanding")
    eg = snapshot.get("earnings_growth")
    price = snapshot.get("last_price")

    if eps is None or shares is None or eps <= 0 or shares <= 0:
        return None

    net_income = float(eps) * float(shares)
    base_fcf = net_income * cf_conversion_factor

    # Pick a sensible growth rate: clip earnings_growth to a reasonable band
    if eg is not None and -0.30 <= float(eg) <= 0.50:
        g1 = float(eg)
    else:
        g1 = 0.08  # neutral default
    g2 = max(0.03, g1 / 2)  # fade toward GDP
    terminal_g = 0.025
    wacc_default = 0.09

    return DCFInputs(
        base_fcf=base_fcf,
        growth_y1_5=g1,
        growth_y6_10=g2,
        terminal_growth=terminal_g,
        wacc=wacc_default,
        shares_outstanding=float(shares),
        current_price=float(price) if price else 0.0,
    )
