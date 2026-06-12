"""
Pure pandas snapshot math for the screener — no I/O, unit-tested.

compute_snapshot() turns date×symbol OHLCV panels (from the breadth bars
store) into one indicator row per symbol. All windows are trailing; the
rolling high/low *levels* are shifted one day so a cross by today's (or a
live intraday) price is detectable against yesterday's level.
"""

import numpy as np
import pandas as pd

# ≈320 trading bars: 252 for 52-week levels + warmup buffer for SMA200/RSI.
SNAPSHOT_LOOKBACK_DAYS = 470

SESSION_MINUTES = 390          # 9:30–16:00 ET
MIN_SESSION_FRACTION = 0.02    # floor stops open-bell RVOL blowups


def rsi(close, n=14):
    """Wilder RSI per column of a date×symbol panel (or a Series)."""
    delta = close.diff()
    up    = delta.clip(lower=0)
    down  = (-delta).clip(lower=0)
    avg_up   = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_down = down.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_up / avg_down
    out = 100 - 100 / (1 + rs)
    # down-avg of zero → rs inf → rsi 100; 0/0 stays NaN, which is correct
    return out.where(~np.isinf(rs), 100.0)


def atr(high, low, close, n=14):
    """Wilder ATR per column of date×symbol panels."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        keys=["hl", "hc", "lc"],
    ).groupby(level=1).max()
    tr = tr.reindex(close.index)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def compute_snapshot(close, volume, open_, high, low, spy_close):
    """→ DataFrame, one row per symbol (SNAPSHOT_COLS minus what store adds).

    Panels: date×symbol, ascending date index of YYYY-MM-DD strings.
    spy_close: Series of SPY closes indexed by date (aligned/ffilled here).
    Symbols whose last bar is older than the panel's newest date come out
    mostly NaN — stale names never match a filter, by design.
    """
    if close.empty:
        return pd.DataFrame()

    chg_pct     = close.pct_change(fill_method=None) * 100
    gap_pct     = (open_ / close.shift(1) - 1) * 100
    vol_chg_pct = volume.pct_change(fill_method=None) * 100

    # avg includes the last completed bar (the right base for the NEXT live
    # day); the stored EOD rvol divides by the average of the 10 days BEFORE
    # the snapshot bar so the spike day doesn't dampen its own ratio.
    avg_vol_10d  = volume.rolling(10, min_periods=10).mean()
    rvol_10d     = volume / avg_vol_10d.shift(1)

    smas = {n: close.rolling(n, min_periods=n).mean() for n in (20, 50, 150, 200)}

    rsi14   = rsi(close, 14)
    atr14   = atr(high, low, close, 14)
    atr_pct = atr14 / close * 100

    spy = spy_close.reindex(close.index).ffill()
    rs_parts = {}
    for label, bars in (("rs_1m_pct", 21), ("rs_3m_pct", 63)):
        sym_ret = (close / close.shift(bars) - 1) * 100
        spy_ret = (spy / spy.shift(bars) - 1) * 100
        rs_parts[label] = sym_ret.sub(spy_ret, axis=0)

    # Cross-detection levels exclude today; %-off-52w uses the inclusive
    # extreme so a fresh high reads as 0, not a positive overshoot.
    high_20d = high.rolling(20, min_periods=20).max().shift(1)
    low_20d  = low.rolling(20, min_periods=20).min().shift(1)
    high_252 = high.rolling(252, min_periods=60).max().shift(1)
    low_252  = low.rolling(252, min_periods=60).min().shift(1)
    hi252_incl = high.rolling(252, min_periods=60).max()
    lo252_incl = low.rolling(252, min_periods=60).min()
    pct_from_52w_high = (close / hi252_incl - 1) * 100
    pct_from_52w_low  = (close / lo252_incl - 1) * 100

    last = {
        "close": close, "open": open_, "volume": volume,
        "chg_pct": chg_pct, "gap_pct": gap_pct, "vol_chg_pct": vol_chg_pct,
        "avg_vol_10d": avg_vol_10d, "rvol_10d": rvol_10d,
        "sma20": smas[20], "sma50": smas[50], "sma150": smas[150], "sma200": smas[200],
        "rsi14": rsi14, "atr14": atr14, "atr_pct": atr_pct,
        "rs_1m_pct": rs_parts["rs_1m_pct"], "rs_3m_pct": rs_parts["rs_3m_pct"],
        "high_20d": high_20d, "low_20d": low_20d,
        "high_252": high_252, "low_252": low_252,
        "pct_from_52w_high": pct_from_52w_high,
        "pct_from_52w_low": pct_from_52w_low,
    }
    out = pd.DataFrame({k: v.iloc[-1] for k, v in last.items()})
    out.index.name = "symbol"
    out.insert(0, "date", close.apply(lambda s: s.last_valid_index()))
    return out.reset_index()


def session_fraction(now_et):
    """Fraction of the regular session elapsed at `now_et` (datetime)."""
    minutes = (now_et.hour - 9) * 60 + now_et.minute - 30
    return min(max(minutes / SESSION_MINUTES, MIN_SESSION_FRACTION), 1.0)


def live_rvol(total_volume, avg_vol_10d, fraction):
    """Today's running volume vs the session-prorated 10-day average."""
    if not total_volume or not avg_vol_10d or avg_vol_10d <= 0 or not fraction:
        return None
    return total_volume / (avg_vol_10d * fraction)
