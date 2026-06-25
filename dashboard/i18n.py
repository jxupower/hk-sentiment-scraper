"""Dashboard i18n — single source of truth for all human-facing strings.

The dashboard supports an English / 中文 toggle in the top-right header. This
module holds two parallel dicts (`EN`, `ZH`) keyed by short slug, and a single
accessor `T(key, lang, **fmt)` that returns the formatted string. EN is the
authoritative key set; ZH falls back to EN on missing keys with a `[missing]`
marker so gaps are visible during QA.

Key naming convention: `surface.thing.kind` — e.g. `tab.screener`,
`screener.btn.refresh`, `backtest.stat.maxdd`. Stable slugs let translators
and code reviewers reason about a key without seeing the surrounding context.

What is NOT translated through this module:
  - Ticker symbols ("0700.HK", "&BANKS", "^HSI") — DB identifiers.
  - Stock names from `securities.name` — DB-sourced display, preserved.
  - Article titles + descriptions from RSS / Yahoo / Reddit — original language.
  - LLM-generated content (Claude business summaries, SWOT defaults, DCF
    walkthrough prose, factor breakdown explanations, devil's-advocate AI) —
    locked English by design; re-prompting Claude in Chinese is a follow-up.
  - Numbers, percentages, ISO dates.
  - Sub-sector / parent-sector labels — those live in `config/sub_sectors.yaml`
    and are resolved by `config.settings.get_subsector_label(name, lang)`.

Usage:
    from dashboard.i18n import T
    T("tab.screener", "en")              # → "Screener"
    T("tab.screener", "zh")              # → "筛选器"
    T("screener.row_count", "en",
       count=42, total=2800)             # → "42 of 2,800 matching filters"
"""
from __future__ import annotations


# ============================================================================
# English (authoritative key set)
# ============================================================================

EN: dict[str, str] = {
    # ---- Global chrome + brand ----
    "app.title":              "Croissant Stock Analyser",
    "app.tagline":            " · Sentiment + Fundamentals + Backtest",
    "app.last_updated":       "Last updated: ",
    "lang.en":                "EN",
    "lang.zh":                "中文",

    # ---- Tabs ----
    "tab.screener":           "Screener",
    "tab.discovery":          "Discovery",
    "tab.screens":            "Screens",
    "tab.backtest":           "Backtest",
    "tab.research":           "Stock Research",
    "tab.risk":               "Risk Forecast",
    "tab.portfolio":          "Portfolio",
    "tab.sentiment":          "Sentiment",

    # ---- Sentiment tab ----
    "sentiment.controls":             "Controls",
    "sentiment.btn.refresh":          "Refresh Now",
    "sentiment.selected":             "Selected: ",
    "sentiment.no_selection":         "(none)",
    "sentiment.sector_detail":        "Sector Detail",
    "sentiment.placeholder":          "Click a sector card above to see detailed analysis.",
    "sentiment.ticker_breakdown":     "Ticker Breakdown (within sector)",
    "sentiment.ai_analysis":          "AI Sector Analysis",
    "sentiment.recent_articles":      "Recent Articles",

    # ---- Common ----
    "common.refresh":         "Refresh",
    "common.clear":           "Clear",
    "common.load":            "Load",
    "common.save":            "Save",
    "common.cancel":          "Cancel",
    "common.delete":          "Delete",
    "common.export":          "Export",
    "common.no_data":         "No data yet.",
    "common.loading":         "Loading...",
    "common.search":          "Search",
    "common.all":             "All",
    "common.none":            "None",
    "common.yes":             "Yes",
    "common.no":              "No",
    "common.median":          "Median",
    "common.mean":            "Mean",
    "common.cap_weighted":    "Cap-weighted",

    # ============================================================
    # Screener tab
    # ============================================================
    # Stat blocks
    "screener.stat.universe":          "Universe size",
    "screener.stat.with_data":         "With fundamentals",
    "screener.stat.latest":            "Latest snapshot",

    # Buttons
    "screener.btn.refresh":            "Refresh",
    "screener.btn.refresh_prices":     "Refresh prices now",
    "screener.btn.clear_filters":      "Clear filters",
    "screener.btn.load_subsector_chart": "Load Sub-Sector P/E Chart",

    # Filter card / accordion
    "screener.filters":                "Filters",
    "screener.accordion.search":       "Search",
    "screener.accordion.classification": "Classification",
    "screener.accordion.valuation":    "Valuation",
    "screener.accordion.quality":      "Quality",
    "screener.accordion.size":         "Size",

    # Field labels
    "screener.label.ticker_contains": "Ticker contains",
    "screener.label.name_contains":   "Name contains",
    "screener.label.sector":          "Sector",
    "screener.label.sub_sector":      "Sub-sector",
    "screener.label.min_completeness": "Min data completeness",
    "screener.label.pe_aggregation":  "P/E aggregation",
    "screener.label.trailing_pe":     "Trailing P/E",
    "screener.label.forward_pe":      "Forward P/E",
    "screener.label.pb":              "P/B",
    "screener.label.evebitda":        "EV/EBITDA",
    "screener.label.dividend_yield":  "Dividend yield %",
    "screener.label.roe":             "ROE %",
    "screener.label.earnings_growth": "Earnings growth %",
    "screener.label.de":              "D/E %",
    "screener.label.beta":            "Beta",
    "screener.label.mcap":            "Market cap (B HKD)",

    # Placeholders
    "screener.ph.ticker":             "e.g. 0700, 9988",
    "screener.ph.name":               "e.g. Tencent, semiconductor",
    "screener.ph.all_sectors":        "All sectors",
    "screener.ph.all_subsectors":     "All sub-sectors",

    # Investor presets
    "screener.presets.title":         "Investor presets",
    "screener.presets.subtitle":      "— one-click composite V/Q/G screens; click to load filter ranges",
    "screener.preset.buffett.label":  "Buffett",
    "screener.preset.buffett.title":  "Wonderful Co. at a Fair Price",
    "screener.preset.graham.label":   "Graham",
    "screener.preset.graham.title":   "Defensive Value",
    "screener.preset.lynch.label":    "Lynch GARP",
    "screener.preset.lynch.title":    "Growth at a Reasonable Price",
    "screener.preset.greenblatt.label": "Magic Formula",
    "screener.preset.greenblatt.title": "Greenblatt Magic Formula",
    "screener.preset.druckenmiller.label": "Druckenmiller",
    "screener.preset.druckenmiller.title": "Growth Momentum",

    # Range filter hint
    "screener.range_hint":            "Range filters pass-through tickers with missing data. To exclude them, raise Min data completeness in the Classification group.",

    # Chart card headers
    "screener.chart.sector_pe":       "{agg} P/E by Sector",
    "screener.chart.subsector_pe":    "{agg} P/E by Sub-Sector",
    "screener.chart.defer_blurb":     " — defer-loaded to keep filter changes fast.",

    # Table card
    "screener.table.title":           "Tickers",
    "screener.row_count":             "{count:,} of {total:,} matching filters",

    # Table columns
    "screener.col.ticker":            "Ticker",
    "screener.col.name":              "Name",
    "screener.col.sector":            "Sector",
    "screener.col.sub_sector":        "Sub-sector",
    "screener.col.price":             "Price",
    "screener.col.mcap_b":            "Mkt Cap (B HKD)",
    "screener.col.pe":                "P/E",
    "screener.col.fwd_pe":            "Fwd P/E",
    "screener.col.pb":                "P/B",
    "screener.col.evebitda":          "EV/EBITDA",
    "screener.col.div_yield":         "Div Yield (%)",
    "screener.col.roe":               "ROE (%)",
    "screener.col.de":                "D/E (%)",
    "screener.col.beta":              "Beta",
    "screener.col.completeness":      "Completeness",

    # Status messages
    "screener.status.refresh_started": "Refresh started at {time} — completes in ~5-10 min. Dashboard auto-refresh will pick up new data.",
    "screener.status.refresh_running": "Already running — please wait.",
    "screener.get_price":             "Get price",

    # ============================================================
    # Discovery tab
    # ============================================================
    "discovery.alert.title":          "Discovery, not recommendations. ",
    "discovery.alert.body":           "Candidates for further research, not buy/sell advice. Higher composite percentile = more attractive on the chosen factor weights; viability filters disqualify negative book value, microcaps, extreme P/E, and broken margins.",
    "discovery.stat.scorable":        "Scorable",
    "discovery.stat.disqualified":    "Disqualified",
    "discovery.stat.flagged":         "Flagged",
    "discovery.btn.recompute":        "Recompute",

    "discovery.weights.title":        "Factor Weights",
    "discovery.weights.value":        "Value (cheap)",
    "discovery.weights.quality":      "Quality (ROE, low debt)",
    "discovery.weights.growth":       "Growth (earnings, revenue)",
    "discovery.weights.sentiment":    "Sentiment (news mood)",
    "discovery.weights.normalized":   " (normalized: V {v:.0f}% / Q {q:.0f}% / G {g:.0f}% / S {s:.0f}%)",
    "discovery.weights.zero":         " (weights all zero — please set at least one)",

    "discovery.filter.window":        "Sentiment window (days)",
    "discovery.filter.min_composite": "Min composite percentile",
    "discovery.filter.show":          "Show",
    "discovery.filter.include_flagged": "Include flagged",
    "discovery.filter.include_dq":    "Include disqualified",
    "discovery.filter.sector":        "Sector filter",
    "discovery.dist_title":           "Composite Percentile Distribution",
    "discovery.table.title":          "Discovery Candidates",
    "discovery.row_count":            "{count:,} of {total:,} after filters",

    # Discovery table columns
    "discovery.col.ticker":           "Ticker",
    "discovery.col.name":             "Name",
    "discovery.col.sector":           "Sector",
    "discovery.col.price":            "Price",
    "discovery.col.composite":        "Composite %",
    "discovery.col.value":            "Value %",
    "discovery.col.quality":          "Quality %",
    "discovery.col.growth":           "Growth %",
    "discovery.col.sentiment":        "Sentiment %",
    "discovery.col.articles":         "Articles",
    "discovery.col.pe":               "P/E",
    "discovery.col.roe":              "ROE %",
    "discovery.col.earn_growth":      "Earn Growth %",
    "discovery.col.mcap_b":           "Mkt Cap (B)",
    "discovery.col.status":           "Status",

    # ============================================================
    # Stock Research tab
    # ============================================================
    "research.alert":                 "Single-stock deep research, structured as Richard Coffin / The Plain Bagel's 6-step framework. Honest gaps: DCF uses an FCF proxy (EPS × shares × 0.8) — adjust the slider; akshare data is as-restated, not point-in-time; no capex/insider/management-compensation data; forensic detector is heuristic only.",
    "research.label.ticker":          "Ticker (e.g. 0700.HK)",
    "research.label.status":          "Research status",
    "research.ph.ticker":              "Type to search HK tickers...",
    "research.ph.status":              "(not set)",
    "research.btn.load":              "Load report",
    "research.placeholder":           "Pick a ticker above, or browse a sub-sector composite below.",
    "research.subsector_browse":      "Sub-sector composites",
    "research.subsector_browse.sub":  "— click a row to load the composite report",
    "research.col.ticker":            "Ticker",
    "research.col.subsector":         "Sub-sector",
    "research.col.parent_sector":     "Parent sector",
    "research.col.constituents":      "Constituents",

    # Status enum (statuses themselves stay as enum keys but their labels translate)
    "research.status.raw":             " Raw (not yet researched)",
    "research.status.researched":      " Researched",
    "research.status.watchlist":       " Watchlist (good but expensive)",
    "research.status.owned":           " Owned",
    "research.status.rejected":        " Rejected (researched, decided not to own)",

    # Section titles
    "research.sec1":                   "1. Idea & Screening Context",
    "research.sec2":                   "2. Business Overview",
    "research.sec3":                   "3. Financial Analysis",
    "research.sec3b":                  "3b. Financial Statements",
    "research.sec4":                   "4. Strategy & Management",
    "research.sec5":                   "5. Valuation",
    "research.sec6":                   "6. Notes & Review",

    # Header card stat labels (single stock)
    "research.label.current_price":   "Current price",
    "research.label.mkt_cap":         "Mkt cap: ",
    "research.label.subsector_composite": "Sub-sector composite",

    # Composite aggregate stats
    "research.composite.constituents": "Constituents",
    "research.composite.total_mcap":   "Total mkt cap",
    "research.composite.median_pe":    "Median P/E",
    "research.composite.median_pb":    "Median P/B",
    "research.composite.median_roe":   "Median ROE",
    "research.composite.median_div":   "Median yield",
    "research.composite.summary":      "{name} — {n} active constituents, total mkt cap {mcap}",
    "research.composite.list_title":   "Constituents",
    "research.composite.list_sub":     "— click a ticker to drill into the single-stock view",
    "research.constituent.ticker":     "Ticker",
    "research.constituent.name":       "Name",
    "research.constituent.sector":     "Sector",
    "research.constituent.mcap_b":     "Mkt cap (B)",
    "research.constituent.weight":     "Weight %",
    "research.constituent.pe":         "P/E",
    "research.constituent.pb":         "P/B",
    "research.constituent.roe":        "ROE %",
    "research.constituent.growth":     "Earn growth %",
    "research.constituent.yield":      "Yield %",
    "research.constituent.complete":   "Data %",

    # Section 2 — Business
    "research.business.summary":      "AI business summary",
    "research.swot.strengths":        "Strengths",
    "research.swot.weaknesses":       "Weaknesses",
    "research.swot.opportunities":    "Opportunities",
    "research.swot.threats":          "Threats",
    "research.articles_title":        "Recent articles (30d)",

    # Section 3 — Financial
    "research.cagr_title":            "CAGR",
    "research.peer_title":            "Peer comparison",
    "research.forensic_title":        "Forensic red flags",
    "research.btn.load_financial_statements": "Load Financial Statements",
    "research.fs.income":             "Income",
    "research.fs.balance":            "Balance Sheet",
    "research.fs.cashflow":           "Cash Flow",
    "research.fs.earnings":           "Earnings",
    "research.btn.ai_forensic":       "AI Forensic Review",
    "research.ai_forensic.note":      "AI reviews quantitative line items only — no access to footnotes, MD&A, or auditor letters.",
    "research.ai_forensic.empty":     "No financial statements loaded yet — click above to fetch, then run the review.",
    "research.ai_forensic.unavailable": "Forensic review unavailable: {err}",
    "research.btn.bullbear":          "AI Bull / Bear Stress Test",
    "research.bullbear.note":         "Strongest case for each side, then 3-5 KPIs to monitor over the next 12 months. Not balanced — read both.",
    "research.bullbear.empty":        "Pick a ticker and click Load first, then run the stress test.",
    "research.bullbear.unavailable":  "Bull/Bear stress test unavailable: {err}",

    # Section 4 — Strategy
    "research.label.period":          "Time period",
    "research.label.price_chart":     "Price history",
    "research.chart_style.line":      "Line",
    "research.chart_style.candle":    "Candle",
    "research.label.strategy_notes":  "Strategy notes",

    # Section 5 — Valuation
    "research.label.relval":          "Relative valuation",
    "research.dcf_title":             "DCF calculator",
    "research.dcf.g15":               "Growth Y1-5 (%)",
    "research.dcf.g610":              "Growth Y6-10 (%)",
    "research.dcf.tg":                "Terminal growth (%)",
    "research.dcf.wacc":              "WACC / discount rate (%)",
    "research.dcf.walkthrough":       "DCF walkthrough — every step from your growth rates to the MoS",
    "research.label.valuation_notes": "Valuation notes",

    # Section 6 — Notes
    "research.label.thesis":          "Investment thesis",
    "research.label.devils_advocate": "Devil's-advocate AI",
    "research.btn.counter_args":      "Generate counter-arguments",
    "research.btn.save_notes":        "Save all notes",
    "research.btn.export_md":         "Export as Markdown",

    # ============================================================
    # Backtest tab
    # ============================================================
    "backtest.alert.title":           "Preset + V/Q/G top-10 walk-forward backtest. ",
    "backtest.alert.body1":           "At every rebalance date the engine applies the chosen investor preset to as-of fundamentals, ranks survivors by composite V/Q/G percentile, and holds the top 10 ",
    "backtest.alert.weight_label":    "market-cap-weighted",
    "backtest.alert.body2":           ". Returns are compounded vs ^HSI; Sharpe uses rf = 3%. ",
    "backtest.caveats.title":         "Honest caveats: ",
    "backtest.caveats.body":          "akshare fundamentals are as-restated (60-day reporting lag applied); survivor bias from delisted tickers; no transaction costs; daily rebalances mostly re-equalise cap weights since snapshots are quarterly/annual. Historical akshare snapshots do not carry EV/EBITDA or dividend_yield, so Greenblatt's EV/EBITDA cap and Graham's dividend floor are unenforced in backtests — only Buffett / Lynch / Druckenmiller filter on fields available in the historical data.",

    # Backtest setup card
    "backtest.setup":                 "Backtest setup",
    "backtest.label.preset":          "Investor preset",
    "backtest.label.horizon":         "Time horizon",
    "backtest.label.rebal":           "Rebalance frequency",
    "backtest.label.weight_cap":      "Max position weight",
    "backtest.horizon.1y":            "1 year",
    "backtest.horizon.3y":            "3 years",
    "backtest.horizon.5y":            "5 years",
    "backtest.rebal.1d":              "Daily",
    "backtest.rebal.3d":              "3-day",
    "backtest.rebal.1w":              "Weekly",
    "backtest.rebal.1m":              "Monthly",
    "backtest.btn.run":               "Run backtest",
    "backtest.btn.run_running":       "Running... (~30-60s)",

    # Performance + stats
    "backtest.performance":           "Performance",
    "backtest.stat.total":            "Total return",
    "backtest.stat.annret":           "Annualized",
    "backtest.stat.vol":              "Annualized vol",
    "backtest.stat.sharpe":           "Sharpe (rf=3%)",
    "backtest.stat.maxdd":            "Max drawdown",
    "backtest.stat.hit":              "Hit rate vs ^HSI",
    "backtest.stat.excess":           "Excess vs ^HSI",
    "backtest.stat.turnover":         "Annualized turnover",

    # Charts + tables
    "backtest.section.equity":        "Equity curve vs ^HSI",
    "backtest.section.drawdown":      "Drawdown timeline",
    "backtest.section.sector":        "Sector breakdown",
    "backtest.section.initial":       "Initial holdings",
    "backtest.section.changes":       "Rebalance changes",
    "backtest.section.final":         "Final holdings",

    "backtest.col.ticker":            "Ticker",
    "backtest.col.name":              "Name",
    "backtest.col.price":             "Price",
    "backtest.col.weight":            "Weight %",
    "backtest.col.shares":            "Shares",
    "backtest.col.weight_delta":      "Δ weight",
    "backtest.col.shares_delta":      "Δ shares",
    "backtest.col.date":              "Date",
    "backtest.col.action":            "Action",
    "backtest.col.units":             "Units",

    # Save handoff
    "backtest.save_title":            "Save as portfolio",
    "backtest.save_sub":              "— start-of-period preset survivors, 100 shares each, rf = 3% pre-set",
    "backtest.btn.save":              "Save & open in Portfolio tab",

    # ============================================================
    # Portfolio tab
    # ============================================================
    "portfolio.title":                "Portfolio Rebalancer",
    "portfolio.subtitle":             "Max-Sharpe via Modern Portfolio Theory",
    "portfolio.saved_portfolios":     "Saved portfolios",
    "portfolio.label.name":           "Portfolio name",
    "portfolio.holdings_table":       "Holdings table",
    "portfolio.label.lookback":       "Lookback window",
    "portfolio.label.rebal":          "Rebalance frequency",
    "portfolio.label.weight_cap":     "Weight cap",
    "portfolio.label.rf":             "Risk-free rate",
    "portfolio.btn.compute":          "Compute",
    "portfolio.btn.save_status_quo":  "Save status-quo",
    "portfolio.btn.save_optimal":     "Save w/ optimal",
    "portfolio.btn.delete":           "Delete",
    "portfolio.btn.add_row":          "Add row",
    "portfolio.btn.delete_row":       "Delete row",
    "portfolio.efficient_frontier":   "Efficient frontier",
    "portfolio.backtest_section":     "Backtest walk-forward",
    "portfolio.metric.sharpe":        "Sharpe ratio",
    "portfolio.metric.min_max":       "Min/Max weights",
    # — Extended coverage —
    "portfolio.alert":                ("Portfolio Rebalancer — Max-Sharpe via Modern Portfolio Theory. "
                                        "Enter your holdings (ticker + shares), add candidate tickers with 0 shares, "
                                        "pick a lookback + rebalance frequency, click Compute. The full universe (current + candidates) "
                                        "is fed through Ledoit-Wolf shrunk Σ and SLSQP to find the long-only, capped, max-Sharpe portfolio."),
    "portfolio.alert.gaps":           ("Honest gaps: sample means are very noisy, so 'optimal weights' are a directional guide; "
                                        "in-sample Sharpe is biased up by construction (the walk-forward backtest shows out-of-sample reality); "
                                        "no transaction costs / taxes modelled."),
    "portfolio.saved_hint":           (" — name + Save to persist the current holdings to "
                                        "Supabase. Once saved, they show up as synthetic "
                                        "tickers (e.g. @CORE, @CORE$OPT) in the Risk Forecast tab."),
    "portfolio.label.existing":       "Existing portfolios",
    "portfolio.ph.name":              "UPPERCASE / digits / _ — e.g. CORE",
    "portfolio.ph.saved":             "Load a saved portfolio…",
    "portfolio.btn.save_status_full": "Save status-quo portfolio  →  @NAME",
    "portfolio.btn.save_optimal_full":"Save optimised portfolio  →  @NAME$OPT",
    "portfolio.save_status_blurb":    "Materialises the constant-share buy-and-hold index from the holdings table above.",
    "portfolio.save_optimal_blurb":   ("Materialises the latest max-Sharpe optimal weight series. "
                                        "Requires Compute first (same tickers as the table)."),
    "portfolio.placeholder_text":     "Enter holdings, pick parameters, then click Compute.",
    "portfolio.params_title":         "Parameters",
    "portfolio.hero.status_quo":      "Status quo",
    "portfolio.hero.current_optimum": "Current-only optimum",
    "portfolio.hero.full_optimum":    "Full-universe optimum",
    "portfolio.header.weights":       "Weights — current vs. optimal",
    "portfolio.header.frontier":      "Efficient frontier",
    "portfolio.header.backtest":      "Walk-forward backtest",
    "portfolio.header.candidate":     "Candidate marginal value",
    "portfolio.header.trade_list":    "Rebalance trade list (to reach full-optimal)",
    "portfolio.header.diagnostics":   "Estimation diagnostics",

    # ============================================================
    # Risk Forecast tab
    # ============================================================
    "risk.title":                     "Risk Forecast",
    "risk.subtitle":                  "GJR-GARCH(1,1) with Student-t innovations",
    "risk.label.ticker":              "Ticker (index or HK stock)",
    "risk.label.window":              "History window (fit data)",
    "risk.label.horizon":             "Horizon",
    "risk.horizon.5d":                "5d",
    "risk.horizon.21d":               "21d (1mo)",
    "risk.horizon.63d":               "63d (1qtr)",
    "risk.btn.load":                  "Load",
    "risk.fan_chart":                 "Fan chart",
    "risk.vol_cone":                  "Volatility cone",
    "risk.var_table":                 "VaR / CVaR table",
    "risk.prob_table":                "Probability table",
    "risk.drawdown_hist":             "Drawdown histogram",

    # ============================================================
    # Screens tab
    # ============================================================
    "screens.title":                  "Rule-based screens",
    "screens.passing":                "Passing Tickers",
    "screens.matching":               "Matching Tickers",
}


# ============================================================================
# 中文 (Simplified Mandarin — HK-friendly finance terminology)
# ============================================================================

ZH: dict[str, str] = {
    # ---- Global chrome + brand ----
    "app.title":              "可颂股票分析",
    "app.tagline":            " · 情绪 + 基本面 + 回测",
    "app.last_updated":       "最后更新: ",
    "lang.en":                "EN",
    "lang.zh":                "中文",

    # ---- Tabs ----
    "tab.screener":           "筛选器",
    "tab.discovery":          "发现",
    "tab.screens":            "规则筛选",
    "tab.backtest":           "回测",
    "tab.research":           "个股研究",
    "tab.risk":               "风险预测",
    "tab.portfolio":          "投资组合",
    "tab.sentiment":          "情绪",

    # ---- Sentiment tab ----
    "sentiment.controls":             "控制",
    "sentiment.btn.refresh":          "立即刷新",
    "sentiment.selected":             "已选: ",
    "sentiment.no_selection":         "(未选择)",
    "sentiment.sector_detail":        "板块详情",
    "sentiment.placeholder":          "点击上方板块卡片查看详细分析。",
    "sentiment.ticker_breakdown":     "板块内个股分布",
    "sentiment.ai_analysis":          "AI 板块分析",
    "sentiment.recent_articles":      "最近文章",

    # ---- Common ----
    "common.refresh":         "刷新",
    "common.clear":           "清除",
    "common.load":            "加载",
    "common.save":            "保存",
    "common.cancel":          "取消",
    "common.delete":          "删除",
    "common.export":          "导出",
    "common.no_data":         "暂无数据。",
    "common.loading":         "加载中...",
    "common.search":          "搜索",
    "common.all":             "全部",
    "common.none":            "无",
    "common.yes":             "是",
    "common.no":              "否",
    "common.median":          "中位数",
    "common.mean":            "平均值",
    "common.cap_weighted":    "市值加权",

    # ============================================================
    # Screener tab
    # ============================================================
    "screener.stat.universe":          "标的总数",
    "screener.stat.with_data":         "已采集基本面",
    "screener.stat.latest":            "最新快照",

    "screener.btn.refresh":            "刷新",
    "screener.btn.refresh_prices":     "立即刷新价格",
    "screener.btn.clear_filters":      "清除筛选条件",
    "screener.btn.load_subsector_chart": "加载子板块市盈率图",

    "screener.filters":                "筛选条件",
    "screener.accordion.search":       "搜索",
    "screener.accordion.classification": "分类",
    "screener.accordion.valuation":    "估值",
    "screener.accordion.quality":      "质量",
    "screener.accordion.size":         "规模",

    "screener.label.ticker_contains": "代码包含",
    "screener.label.name_contains":   "名称包含",
    "screener.label.sector":          "行业",
    "screener.label.sub_sector":      "子板块",
    "screener.label.min_completeness": "最低数据完整度",
    "screener.label.pe_aggregation":  "市盈率聚合方式",
    "screener.label.trailing_pe":     "动态市盈率",
    "screener.label.forward_pe":      "预期市盈率",
    "screener.label.pb":              "市净率",
    "screener.label.evebitda":        "企业价值倍数",
    "screener.label.dividend_yield":  "股息率 %",
    "screener.label.roe":             "净资产收益率 %",
    "screener.label.earnings_growth": "盈利增长 %",
    "screener.label.de":              "资产负债率 %",
    "screener.label.beta":            "Beta 系数",
    "screener.label.mcap":            "市值 (十亿港元)",

    "screener.ph.ticker":             "例如 0700, 9988",
    "screener.ph.name":               "例如 腾讯、半导体",
    "screener.ph.all_sectors":        "全部行业",
    "screener.ph.all_subsectors":     "全部子板块",

    "screener.presets.title":         "投资风格预设",
    "screener.presets.subtitle":      "— 一键加载经典 V/Q/G 复合筛选条件",
    "screener.preset.buffett.label":  "巴菲特",
    "screener.preset.buffett.title":  "合理价格的优质公司",
    "screener.preset.graham.label":   "格雷厄姆",
    "screener.preset.graham.title":   "防御型价值",
    "screener.preset.lynch.label":    "林奇 GARP",
    "screener.preset.lynch.title":    "合理价格的成长股",
    "screener.preset.greenblatt.label": "神奇公式",
    "screener.preset.greenblatt.title": "格林布拉特神奇公式",
    "screener.preset.druckenmiller.label": "德鲁肯米勒",
    "screener.preset.druckenmiller.title": "成长动能",

    "screener.range_hint":            "数值范围筛选会保留缺失数据的标的。如需排除,请在'分类'组中提高'最低数据完整度'。",

    "screener.chart.sector_pe":       "{agg}市盈率 (按行业)",
    "screener.chart.subsector_pe":    "{agg}市盈率 (按子板块)",
    "screener.chart.defer_blurb":     " — 延迟加载以保持筛选响应速度。",

    "screener.table.title":           "标的",
    "screener.row_count":             "符合筛选条件的 {count:,} / {total:,}",

    "screener.col.ticker":            "代码",
    "screener.col.name":              "名称",
    "screener.col.sector":            "行业",
    "screener.col.sub_sector":        "子板块",
    "screener.col.price":             "价格",
    "screener.col.mcap_b":            "市值 (十亿港元)",
    "screener.col.pe":                "市盈率",
    "screener.col.fwd_pe":            "预期市盈率",
    "screener.col.pb":                "市净率",
    "screener.col.evebitda":          "企业价值倍数",
    "screener.col.div_yield":         "股息率 (%)",
    "screener.col.roe":               "净资产收益率 (%)",
    "screener.col.de":                "资产负债率 (%)",
    "screener.col.beta":              "Beta",
    "screener.col.completeness":      "数据完整度",

    "screener.status.refresh_started": "{time} 已开始刷新 — 约需 5-10 分钟。仪表板将自动更新。",
    "screener.status.refresh_running": "刷新已在进行中 — 请稍候。",
    "screener.get_price":             "获取价格",

    # ============================================================
    # Discovery tab
    # ============================================================
    "discovery.alert.title":          "发现,而非买入推荐。",
    "discovery.alert.body":           "仅供进一步研究的候选标的,非买卖建议。综合百分位越高表示在所选因子权重下越具吸引力;可行性筛选会剔除净资产为负、微型市值、极端市盈率和利润率破损的标的。",
    "discovery.stat.scorable":        "可评分",
    "discovery.stat.disqualified":    "已剔除",
    "discovery.stat.flagged":         "已标记",
    "discovery.btn.recompute":        "重新计算",

    "discovery.weights.title":        "因子权重",
    "discovery.weights.value":        "价值 (便宜)",
    "discovery.weights.quality":      "质量 (高 ROE、低负债)",
    "discovery.weights.growth":       "成长 (盈利、收入)",
    "discovery.weights.sentiment":    "情绪 (新闻氛围)",
    "discovery.weights.normalized":   " (归一化: V {v:.0f}% / Q {q:.0f}% / G {g:.0f}% / S {s:.0f}%)",
    "discovery.weights.zero":         " (权重全为零 — 请至少设置一个)",

    "discovery.filter.window":        "情绪窗口 (天)",
    "discovery.filter.min_composite": "最低综合百分位",
    "discovery.filter.show":          "显示",
    "discovery.filter.include_flagged": "包含已标记",
    "discovery.filter.include_dq":    "包含已剔除",
    "discovery.filter.sector":        "行业筛选",
    "discovery.dist_title":           "综合百分位分布",
    "discovery.table.title":          "发现候选标的",
    "discovery.row_count":            "筛选后 {count:,} / {total:,}",

    "discovery.col.ticker":           "代码",
    "discovery.col.name":             "名称",
    "discovery.col.sector":           "行业",
    "discovery.col.price":            "价格",
    "discovery.col.composite":        "综合 %",
    "discovery.col.value":            "价值 %",
    "discovery.col.quality":          "质量 %",
    "discovery.col.growth":           "成长 %",
    "discovery.col.sentiment":        "情绪 %",
    "discovery.col.articles":         "文章数",
    "discovery.col.pe":               "市盈率",
    "discovery.col.roe":              "ROE %",
    "discovery.col.earn_growth":      "盈利增长 %",
    "discovery.col.mcap_b":           "市值 (十亿)",
    "discovery.col.status":           "状态",

    # ============================================================
    # Stock Research tab
    # ============================================================
    "research.alert":                 "单股深度研究,采用 Richard Coffin / The Plain Bagel 的 6 步框架。诚实说明:DCF 使用 EPS × 股本 × 0.8 作为自由现金流的代理 — 请调整滑块;akshare 数据为重报数据而非按当时披露,无资本开支/内部交易/管理层薪酬数据;财务造假检测仅为启发式。",
    "research.label.ticker":          "代码 (例如 0700.HK)",
    "research.label.status":          "研究状态",
    "research.ph.ticker":             "输入代码搜索港股...",
    "research.ph.status":             "(未设置)",
    "research.btn.load":              "加载报告",
    "research.placeholder":           "选择上方代码,或浏览下方子板块复合指数。",
    "research.subsector_browse":      "子板块复合指数",
    "research.subsector_browse.sub":  "— 点击行加载该子板块报告",
    "research.col.ticker":            "代码",
    "research.col.subsector":         "子板块",
    "research.col.parent_sector":     "母行业",
    "research.col.constituents":      "成分股",

    "research.status.raw":             " 原始 (未研究)",
    "research.status.researched":      " 已研究",
    "research.status.watchlist":       " 观察名单 (优质但偏贵)",
    "research.status.owned":           " 持有",
    "research.status.rejected":        " 已剔除 (研究后决定不持有)",

    "research.sec1":                   "1. 想法与筛选背景",
    "research.sec2":                   "2. 业务概览",
    "research.sec3":                   "3. 财务分析",
    "research.sec3b":                  "3b. 财务报表",
    "research.sec4":                   "4. 战略与管理",
    "research.sec5":                   "5. 估值",
    "research.sec6":                   "6. 笔记与复盘",

    "research.label.current_price":   "当前价格",
    "research.label.mkt_cap":         "市值: ",
    "research.label.subsector_composite": "子板块复合指数",

    "research.composite.constituents": "成分股数",
    "research.composite.total_mcap":   "总市值",
    "research.composite.median_pe":    "市盈率中位数",
    "research.composite.median_pb":    "市净率中位数",
    "research.composite.median_roe":   "ROE 中位数",
    "research.composite.median_div":   "股息率中位数",
    "research.composite.summary":      "{name} — {n} 个活跃成分股,总市值 {mcap}",
    "research.composite.list_title":   "成分股",
    "research.composite.list_sub":     "— 点击代码查看单股视图",
    "research.constituent.ticker":     "代码",
    "research.constituent.name":       "名称",
    "research.constituent.sector":     "行业",
    "research.constituent.mcap_b":     "市值 (十亿)",
    "research.constituent.weight":     "权重 %",
    "research.constituent.pe":         "市盈率",
    "research.constituent.pb":         "市净率",
    "research.constituent.roe":        "ROE %",
    "research.constituent.growth":     "盈利增长 %",
    "research.constituent.yield":      "股息率 %",
    "research.constituent.complete":   "数据 %",

    "research.business.summary":      "AI 业务摘要",
    "research.swot.strengths":        "优势",
    "research.swot.weaknesses":       "劣势",
    "research.swot.opportunities":    "机会",
    "research.swot.threats":          "威胁",
    "research.articles_title":        "近 30 天文章",

    "research.cagr_title":            "复合年化增长率",
    "research.peer_title":            "同业对比",
    "research.forensic_title":        "财务造假红旗",
    "research.btn.load_financial_statements": "加载财务报表",
    "research.fs.income":             "利润表",
    "research.fs.balance":            "资产负债表",
    "research.fs.cashflow":           "现金流量表",
    "research.fs.earnings":           "盈利",
    "research.btn.ai_forensic":       "AI 法证审查",
    "research.ai_forensic.note":      "AI 仅审查定量数据 — 无法读取附注、管理层讨论或审计意见。",
    "research.ai_forensic.empty":     "尚未加载财务报表 — 请先点击上方加载，再运行审查。",
    "research.ai_forensic.unavailable": "法证审查不可用: {err}",
    "research.btn.bullbear":          "AI 多空压力测试",
    "research.bullbear.note":         "分别给出最强多/空论据，并列出未来 12 个月需关注的 3-5 个关键指标。两边都需阅读，不做调和。",
    "research.bullbear.empty":        "请先选择标的并点击加载，再运行压力测试。",
    "research.bullbear.unavailable":  "多空压力测试不可用: {err}",

    "research.label.period":          "时间区间",
    "research.label.price_chart":     "价格走势",
    "research.chart_style.line":      "折线",
    "research.chart_style.candle":    "K 线",
    "research.label.strategy_notes":  "战略笔记",

    "research.label.relval":          "相对估值",
    "research.dcf_title":             "DCF 计算器",
    "research.dcf.g15":               "第 1-5 年增长率 (%)",
    "research.dcf.g610":              "第 6-10 年增长率 (%)",
    "research.dcf.tg":                "永续增长率 (%)",
    "research.dcf.wacc":              "WACC / 折现率 (%)",
    "research.dcf.walkthrough":       "DCF 计算逐步演示 — 从增长率到安全边际",
    "research.label.valuation_notes": "估值笔记",

    "research.label.thesis":          "投资论点",
    "research.label.devils_advocate": "AI 反方观点",
    "research.btn.counter_args":      "生成反向论据",
    "research.btn.save_notes":        "保存所有笔记",
    "research.btn.export_md":         "导出 Markdown",

    # ============================================================
    # Backtest tab
    # ============================================================
    "backtest.alert.title":           "预设 + V/Q/G 前 10 名滚动回测。",
    "backtest.alert.body1":           "在每个再平衡日,引擎将所选投资风格预设应用到当时的基本面数据,按综合 V/Q/G 百分位对幸存者排序,持有前 10 名的",
    "backtest.alert.weight_label":    "市值加权组合",
    "backtest.alert.body2":           "。回报相对于 ^HSI 复合计算;夏普比率使用 rf = 3%。",
    "backtest.caveats.title":         "诚实说明: ",
    "backtest.caveats.body":          "akshare 基本面数据为重报数据(应用 60 天披露延迟);存在退市标的的幸存者偏差;未计入交易成本;由于快照为季度/年度,每日再平衡主要是重新加权。历史 akshare 快照不包含企业价值倍数或股息率,因此格林布拉特的 EV/EBITDA 上限和格雷厄姆的股息下限在回测中不会被强制执行 — 只有巴菲特/林奇/德鲁肯米勒使用历史数据中可用的字段。",

    "backtest.setup":                 "回测设置",
    "backtest.label.preset":          "投资风格预设",
    "backtest.label.horizon":         "时间区间",
    "backtest.label.rebal":           "再平衡频率",
    "backtest.label.weight_cap":      "单仓位上限",
    "backtest.horizon.1y":            "1 年",
    "backtest.horizon.3y":            "3 年",
    "backtest.horizon.5y":            "5 年",
    "backtest.rebal.1d":              "每日",
    "backtest.rebal.3d":              "3 日",
    "backtest.rebal.1w":              "每周",
    "backtest.rebal.1m":              "每月",
    "backtest.btn.run":               "运行回测",
    "backtest.btn.run_running":       "运行中... (约 30-60 秒)",

    "backtest.performance":           "业绩",
    "backtest.stat.total":            "总回报",
    "backtest.stat.annret":           "年化回报",
    "backtest.stat.vol":              "年化波动率",
    "backtest.stat.sharpe":           "夏普比率 (rf=3%)",
    "backtest.stat.maxdd":            "最大回撤",
    "backtest.stat.hit":              "对 ^HSI 胜率",
    "backtest.stat.excess":           "相对 ^HSI 超额",
    "backtest.stat.turnover":         "年化换手率",

    "backtest.section.equity":        "净值曲线 vs ^HSI",
    "backtest.section.drawdown":      "回撤时间序列",
    "backtest.section.sector":        "行业分布",
    "backtest.section.initial":       "初始持仓",
    "backtest.section.changes":       "再平衡变动",
    "backtest.section.final":         "最终持仓",

    "backtest.col.ticker":            "代码",
    "backtest.col.name":              "名称",
    "backtest.col.price":             "价格",
    "backtest.col.weight":            "权重 %",
    "backtest.col.shares":            "股数",
    "backtest.col.weight_delta":      "Δ 权重",
    "backtest.col.shares_delta":      "Δ 股数",
    "backtest.col.date":              "日期",
    "backtest.col.action":            "操作",
    "backtest.col.units":             "单位数",

    "backtest.save_title":            "保存为投资组合",
    "backtest.save_sub":              "— 起始日的预设幸存者,各 100 股,rf = 3% 已预设",
    "backtest.btn.save":              "保存并打开投资组合页",

    # ============================================================
    # Portfolio tab
    # ============================================================
    "portfolio.title":                "投资组合再平衡",
    "portfolio.subtitle":             "基于现代投资组合理论的最大夏普",
    "portfolio.saved_portfolios":     "已保存组合",
    "portfolio.label.name":           "组合名称",
    "portfolio.holdings_table":       "持仓表",
    "portfolio.label.lookback":       "回看窗口",
    "portfolio.label.rebal":          "再平衡频率",
    "portfolio.label.weight_cap":     "单仓位上限",
    "portfolio.label.rf":             "无风险利率",
    "portfolio.btn.compute":          "计算",
    "portfolio.btn.save_status_quo":  "保存现状",
    "portfolio.btn.save_optimal":     "保存含最优权重",
    "portfolio.btn.delete":           "删除",
    "portfolio.btn.add_row":          "添加行",
    "portfolio.btn.delete_row":       "删除行",
    "portfolio.efficient_frontier":   "有效前沿",
    "portfolio.backtest_section":     "滚动回测",
    "portfolio.metric.sharpe":        "夏普比率",
    "portfolio.metric.min_max":       "权重最小/最大",
    # — Extended coverage —
    "portfolio.alert":                ("投资组合再平衡 — 基于现代投资组合理论的最大夏普求解。"
                                        "输入您的持仓(代码 + 股数),候选标的填 0 股,选择回看期 + 再平衡频率,点击计算。"
                                        "完整宇宙(当前 + 候选)经过 Ledoit-Wolf 协方差收缩 + SLSQP,求解长仓且单仓位有上限的最大夏普组合。"),
    "portfolio.alert.gaps":           ("透明的缺陷:样本均值噪声大,因此“最优权重”是方向性参考;"
                                        "样本内夏普会因构造被高估(滚动回测才反映样本外真实表现);"
                                        "未建模交易成本 / 税。"),
    "portfolio.saved_hint":           " — 输入名称并保存,将当前持仓持久化到 Supabase。保存后会在风险预测页以合成代码(如 @CORE、@CORE$OPT)出现。",
    "portfolio.label.existing":       "已存在的组合",
    "portfolio.ph.name":              "大写字母 / 数字 / _ — 如 CORE",
    "portfolio.ph.saved":             "加载已保存的组合…",
    "portfolio.btn.save_status_full": "保存现状组合  →  @NAME",
    "portfolio.btn.save_optimal_full":"保存最优组合  →  @NAME$OPT",
    "portfolio.save_status_blurb":    "根据上方持仓表生成等股数的买入并持有指数。",
    "portfolio.save_optimal_blurb":   "生成最近一次计算的最大夏普权重序列。需先点击计算(代码必须与表格一致)。",
    "portfolio.placeholder_text":     "输入持仓、选择参数,然后点击计算。",
    "portfolio.params_title":         "参数",
    "portfolio.hero.status_quo":      "现状",
    "portfolio.hero.current_optimum": "当前持仓最优",
    "portfolio.hero.full_optimum":    "全宇宙最优",
    "portfolio.header.weights":       "权重 — 当前 vs. 最优",
    "portfolio.header.frontier":      "有效前沿",
    "portfolio.header.backtest":      "滚动回测",
    "portfolio.header.candidate":     "候选标的边际价值",
    "portfolio.header.trade_list":    "再平衡交易清单(达到全宇宙最优)",
    "portfolio.header.diagnostics":   "估计诊断",

    # ============================================================
    # Risk Forecast tab
    # ============================================================
    "risk.title":                     "风险预测",
    "risk.subtitle":                  "GJR-GARCH(1,1) + Student-t 创新项",
    "risk.label.ticker":              "代码 (指数或港股)",
    "risk.label.window":              "历史窗口 (拟合数据)",
    "risk.label.horizon":             "预测期",
    "risk.horizon.5d":                "5 日",
    "risk.horizon.21d":               "21 日 (1 个月)",
    "risk.horizon.63d":               "63 日 (1 个季度)",
    "risk.btn.load":                  "加载",
    "risk.fan_chart":                 "扇形图",
    "risk.vol_cone":                  "波动率锥",
    "risk.var_table":                 "VaR / CVaR 表",
    "risk.prob_table":                "概率表",
    "risk.drawdown_hist":             "回撤分布直方图",

    # ============================================================
    # Screens tab
    # ============================================================
    "screens.title":                  "规则筛选",
    "screens.passing":                "符合的标的",
    "screens.matching":               "匹配的标的",
}


# ============================================================================
# Accessor
# ============================================================================

def T(key: str, lang: str = "en", **fmt) -> str:
    """Fetch and format a translated string.

    Falls back to the English template when the ZH dict is missing a key.
    Falls back further to a `[missing: <key>]` placeholder when the EN dict
    is also missing — makes gaps visible during QA without crashing.
    """
    table = ZH if lang == "zh" else EN
    template = table.get(key)
    if template is None:
        template = EN.get(key)
    if template is None:
        return f"[missing: {key}]"
    if not fmt:
        return template
    try:
        return template.format(**fmt)
    except (KeyError, IndexError, ValueError):
        # Bad format args shouldn't crash the dashboard — surface the raw
        # template plus a marker so QA notices.
        return f"{template} [bad-fmt]"
