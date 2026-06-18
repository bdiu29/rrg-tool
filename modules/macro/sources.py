"""
Macro data sources — the fetch/cache layer for the market-health dashboard.

Like every consumer in this project the imports up are LAZY + fail-soft: market
proxies come through `rrg.signal._fetch_close` (the 10-min-cached yfinance path —
any ticker, incl. `^VIX` / futures), macro series through `news.sources.fred_observations`
(the FRED key already in .env), and breadth internals through `breadth.build_summary`.
Nothing here computes a signal — it just returns clean pandas Series / dicts that
`indicators.py` + `regime.py` turn into reads. Every leg degrades to None on failure
so the dashboard never blocks on one bad draw (the breadth/news pattern).
"""

import time

import numpy as np
import pandas as pd

# yfinance proxies pulled in ONE batched download (the cached _fetch_close path).
# Futures (HG=F/GC=F) are flaky on yfinance, so ETF fallbacks ride along and the
# consumer picks whichever returned data.
MARKET_SYMBOLS = [
    "SPY", "RSP", "IWM",                         # tape + breadth-of-tape + small caps
    "^VIX", "^VIX3M",                            # vol level + term structure
    "XLK", "XLY", "XLC",                         # growth / offensive baskets
    "XLU", "XLP", "XLV",                         # defensive baskets
    "XLE", "XLF", "XLI",                         # cyclicals (reflation read)
    "HG=F", "GC=F", "CPER", "GLD",               # copper / gold (+ ETF fallbacks)
    "HYG",                                       # high-yield credit (HYG/SPY risk-appetite ratio)
    "DX-Y.NYB", "UUP",                           # US dollar index (+ UUP ETF fallback)
    "BTC-USD",                                   # bitcoin — global liquidity / risk-appetite proxy
]

# FRED series (level, `lin`). Treasury legs reuse the same ids as the news Rates tab.
FRED_SERIES = {
    "T10Y2Y":  "10Y-2Y spread (pp)",
    "T10Y3M":  "10Y-3M spread (pp)",
    "DGS10":   "10-Year yield",
    "DFII10":  "10-Year real yield",
    "T10YIE":  "10-Year breakeven inflation",
    "BAMLH0A0HYM2": "High-yield OAS (credit spread)",
    "ICSA":    "Initial jobless claims",
}

_TTL = 30 * 60                                   # 30 min — the news ensure_fresh cadence
_CACHE = {"at": 0.0, "data": None}


def _market_close():
    """Batched yfinance closes for MARKET_SYMBOLS (2y daily), fail-soft → empty df.
    2y gives the 200d MA / 62w EMA room and a fuller trailing z-score distribution."""
    try:
        from modules.rrg import signal as rrg_signal
        close = rrg_signal._fetch_close(MARKET_SYMBOLS, "1d", "2y")
        if close is None or close.empty:
            return pd.DataFrame()
        # BTC-USD trades weekends; drop weekend rows so the panel index + as_of stay on
        # trading days (the equity/ETF columns would otherwise be forward-filled onto Sat/Sun).
        return close[close.index.dayofweek < 5]
    except Exception:
        return pd.DataFrame()


def _fred_series():
    """{series: ascending pandas Series} from FRED, fail-soft (a missing key → {})."""
    out = {}
    try:
        from modules.news import sources as news_sources
        for sid in FRED_SERIES:
            obs = news_sources.fred_observations(sid, "lin", limit=60) or {}
            if obs:
                s = pd.Series(obs, dtype=float)
                s.index = pd.to_datetime(s.index)
                out[sid] = s.sort_index()
    except Exception:
        pass
    return out


def _breadth():
    """The breadth module's daily readout (regime, McClellan, A/D, highs-lows,
    %above MAs), or None if no backfill exists yet."""
    try:
        from modules.breadth import build_summary
        s = build_summary("sp500")
        return s if s and s.get("regime") is not None else (s or None)
    except Exception:
        return None


def fetch_raw(force=False):
    """All raw inputs, cached for the TTL. Returns:
        { close: DataFrame, fred: {sid: Series}, breadth: dict|None,
          as_of: str|None, ok: {market, fred, breadth} }.
    Each leg is independent so a single failure doesn't sink the others."""
    now = time.time()
    if not force and _CACHE["data"] is not None and (now - _CACHE["at"]) < _TTL:
        return _CACHE["data"]

    close   = _market_close()
    fred    = _fred_series()
    breadth = _breadth()

    as_of = None
    if not close.empty:
        as_of = str(pd.Timestamp(close.index[-1]).date())

    data = {
        "close":   close,
        "fred":    fred,
        "breadth": breadth,
        "as_of":   as_of,
        "ok": {
            "market":  not close.empty,
            "fred":    bool(fred),
            "breadth": breadth is not None and breadth.get("regime") is not None,
        },
    }
    _CACHE.update(at=now, data=data)
    return data


# ---------------------------------------------------------------------------
# Small series helpers reused by indicators.py + regime.py
# ---------------------------------------------------------------------------

def col(close, sym):
    """A clean (NaN-dropped) Series for one symbol, or None if absent/empty."""
    if close is None or sym not in getattr(close, "columns", []):
        return None
    s = close[sym].dropna()
    return s if len(s) else None


def col_any(close, *syms):
    """The first symbol that has data — for futures with an ETF fallback
    (HG=F → CPER). Avoids `col(a) or col(b)`, which raises on a non-empty Series."""
    for sym in syms:
        s = col(close, sym)
        if s is not None:
            return s
    return None


def ratio(close, a, b):
    """The a/b relative-strength line (aligned, NaN-dropped), or None."""
    sa, sb = col(close, a), col(close, b)
    if sa is None or sb is None:
        return None
    r = (sa / sb).dropna()
    return r if len(r) else None


def basket(close, syms):
    """Equal-weight cumulative index of `syms` (mean of daily returns), or None —
    the themes-module construction, reused so a $400 name can't dominate a $30 one."""
    cols = [col(close, s) for s in syms]
    cols = [c for c in cols if c is not None]
    if not cols:
        return None
    df = pd.concat(cols, axis=1).dropna(how="all")
    if df.empty:
        return None
    idx = (1 + df.pct_change().mean(axis=1).fillna(0)).cumprod() * 100
    return idx


def pct_change_n(series, n):
    """`n`-bar percentage change of the last value, or None."""
    if series is None or len(series) <= n:
        return None
    a, b = float(series.iloc[-1]), float(series.iloc[-1 - n])
    if b == 0 or np.isnan(a) or np.isnan(b):
        return None
    return (a / b - 1) * 100


def zscore_mom(series, win=20, lookback=180):
    """Z-score of the series' `win`-bar momentum against its own trailing
    `lookback` distribution — a self-normalizing 'how unusual is this move' read
    used by the regime feature blend. None when there isn't enough history."""
    if series is None or len(series) < win + 30:
        return None
    mom = series.pct_change(win).dropna()
    if len(mom) < 30:
        return None
    ref = mom.iloc[-lookback:]
    mu, sd = float(ref.mean()), float(ref.std())
    if sd == 0 or np.isnan(sd):
        return None
    return float((mom.iloc[-1] - mu) / sd)


def zscore_level(series, lookback=180):
    """Z-score of the latest LEVEL vs its trailing distribution (for spread /
    breakeven / claims series where the level, not the momentum, is the read)."""
    if series is None or len(series) < 30:
        return None
    ref = series.iloc[-lookback:]
    mu, sd = float(ref.mean()), float(ref.std())
    if sd == 0 or np.isnan(sd):
        return None
    return float((float(series.iloc[-1]) - mu) / sd)
