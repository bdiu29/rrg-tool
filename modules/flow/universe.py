"""
The options-flow watch universe = a curated set of high-options-volume names
(where unusual flow is meaningful and liquid) UNIONed with the user's focus list
(Schwab positions ∪ screener watchlists). Editable at runtime via the `universe`
setting; the curated seed below is the default.

Each name is one Schwab chain call per poll, so the list is sized for a ~minute
sweep under the 110/min budget. Tune in settings, not here.
"""

# Liquid, high-options-volume underlyings (mega-cap tech, popular movers, key ETFs).
CURATED = [
    # mega-cap / most-active single names
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX", "AVGO",
    "CRM", "ORCL", "ADBE", "INTC", "MU", "QCOM", "PLTR", "SMCI", "ARM", "COIN",
    "MSTR", "BABA", "JPM", "BAC", "GS", "V", "MA", "DIS", "BA", "CAT",
    "XOM", "CVX", "WMT", "COST", "HD", "LLY", "UNH", "PFE", "MRNA", "JNJ",
    "UBER", "ABNB", "SHOP", "PYPL", "SQ", "SOFI", "RIVN", "F", "GM", "GE",
    # liquid index / sector / thematic ETFs
    "SPY", "QQQ", "IWM", "DIA", "SMH", "XLK", "XLF", "XLE", "XLV", "ARKK",
    "TLT", "GLD", "SLV", "HYG", "EEM",
]


def merge_universe(focus, custom=None):
    """Return the sorted, de-duped poll universe = curated (or `custom` override) ∪ focus."""
    base = custom if custom else CURATED
    syms = {s.strip().upper() for s in list(base) + list(focus or []) if s and s.strip()}
    return sorted(syms)
