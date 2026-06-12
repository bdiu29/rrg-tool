"""
Breadth indicator math — pure pandas functions, unit-tested in
tests/test_breadth_indicators.py.

Note on the McClellan Oscillator: this implementation is RATIO-ADJUSTED
(EMAs of 1000·(adv−dec)/(adv+dec)) rather than the classic EMAs of raw net
advances. Deliberate deviation: universes here are swappable at runtime
(S&P 500 ≈ 500 issues vs Nasdaq ≈ 3,000), and the raw form scales with issue
count, which would break cross-universe comparability and the regime
thresholds. Shape matches the classic; scale is universe-independent.
"""

import numpy as np
import pandas as pd

# McClellan EMA spans (the canonical 19/39-day pair)
EMA_FAST = 19
EMA_SLOW = 39

MA_WINDOWS      = (20, 50, 200)   # for % of stocks above N-day SMA
EMA_WINDOWS     = (5, 10, 20)     # for count/% of stocks above N-day EMA (short-term thrust)
NH_NL_WINDOW    = 252             # 52-week new highs / new lows
HL_INDEX_SMOOTH = 10              # High-Low Index = 10-day SMA of NH/(NH+NL)

# Zweig Breadth Thrust: 10-day EMA of adv/(adv+dec) travelling from below
# 0.40 to above 0.615 within at most 10 trading days.
ZBT_SPAN     = 10
ZBT_LOW      = 0.40
ZBT_HIGH     = 0.615
ZBT_MAX_DAYS = 10


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


# ---------------------------------------------------------------------------
# Cross-sectional daily aggregates
# ---------------------------------------------------------------------------

def daily_aggregates(close, volume):
    """close/volume: date-indexed DataFrames, one column per symbol.

    A symbol only counts toward a metric when it has the history that metric
    needs (e.g. 200 bars for pct_above_200) — denominators are per-day
    eligible counts, so thin early history doesn't distort percentages.
    """
    close  = close.sort_index()
    volume = volume.reindex_like(close)

    prev  = close.shift(1)
    valid = close.notna() & prev.notna()
    chg   = close - prev
    up    = (chg > 0) & valid
    down  = (chg < 0) & valid

    out = pd.DataFrame(index=close.index)
    out["advances"]  = up.sum(axis=1)
    out["declines"]  = down.sum(axis=1)
    out["unchanged"] = ((chg == 0) & valid).sum(axis=1)
    out["up_vol"]    = volume.where(up).sum(axis=1)
    out["down_vol"]  = volume.where(down).sum(axis=1)

    for w in MA_WINDOWS:
        sma      = close.rolling(w, min_periods=w).mean()
        eligible = sma.notna() & close.notna()
        above    = (close > sma) & eligible
        denom    = eligible.sum(axis=1).replace(0, np.nan)
        out[f"pct_above_{w}"] = 100.0 * above.sum(axis=1) / denom

    # Short-term EMA thrust: count and % of names above their 5/10/20-day EMA.
    # Same eligible-denominator discipline as the SMA block above.
    for w in EMA_WINDOWS:
        ema      = close.ewm(span=w, adjust=False, min_periods=w).mean()
        eligible = ema.notna() & close.notna()
        above    = (close > ema) & eligible
        denom    = eligible.sum(axis=1).replace(0, np.nan)
        out[f"n_above_{w}ema"]   = above.sum(axis=1)
        out[f"pct_above_{w}ema"] = 100.0 * above.sum(axis=1) / denom

    roll_max = close.rolling(NH_NL_WINDOW, min_periods=NH_NL_WINDOW).max()
    roll_min = close.rolling(NH_NL_WINDOW, min_periods=NH_NL_WINDOW).min()
    out["new_highs"] = ((close >= roll_max) & roll_max.notna()).sum(axis=1)
    out["new_lows"]  = ((close <= roll_min) & roll_min.notna()).sum(axis=1)

    out["n_symbols"] = valid.sum(axis=1)
    return out[out["n_symbols"] > 0]


# ---------------------------------------------------------------------------
# Derived indicator chains
# ---------------------------------------------------------------------------

def derive(agg):
    """agg: output of daily_aggregates (or the breadth_daily table)."""
    adv, dec = agg["advances"].astype(float), agg["declines"].astype(float)
    upv, dnv = agg["up_vol"].astype(float), agg["down_vol"].astype(float)

    out   = pd.DataFrame(index=agg.index)
    denom = (adv + dec).replace(0, np.nan)

    rana             = 1000.0 * (adv - dec) / denom    # ratio-adjusted net advances
    ema_fast         = ema(rana, EMA_FAST)
    ema_slow         = ema(rana, EMA_SLOW)
    out["mcclellan"] = ema_fast - ema_slow
    # Summation Index: day-over-day increment is exactly the oscillator (the
    # classic cumulative definition), but the closed form is used instead of
    # cumsum because cumsum carries a permanent −10·rana₀ seed artifact — the
    # arbitrary first day of stored history would offset the level forever.
    # Steady-state level ≈ 10× average rana (persistent breadth bias shows as
    # level). No +1000 offset.
    out["summation"] = 19.0 * ema_slow - 9.0 * ema_fast

    ad_ratio            = adv / dec.replace(0, np.nan)
    ud_vol              = upv / dnv.replace(0, np.nan)
    out["trin"]         = ad_ratio / ud_vol
    out["ud_vol_ratio"] = ud_vol
    out["net_up_vol"]   = upv - dnv

    out["ad_line"]     = (adv - dec).cumsum()
    out["ad_vol_line"] = (upv - dnv).cumsum()

    nh, nl          = agg["new_highs"].astype(float), agg["new_lows"].astype(float)
    out["nh_nl"]    = nh - nl
    hl_pct          = 100.0 * nh / (nh + nl).replace(0, np.nan)
    out["hl_index"] = hl_pct.rolling(HL_INDEX_SMOOTH, min_periods=1).mean()

    out["zbt_ema"] = ema(adv / denom, ZBT_SPAN)
    return out


def zbt_events(zbt_ema):
    """Dates where a Zweig Breadth Thrust completed."""
    vals, idx = zbt_ema.to_numpy(), zbt_ema.index
    events = []
    for i in range(1, len(vals)):
        if vals[i] >= ZBT_HIGH and vals[i - 1] < ZBT_HIGH:
            window = vals[max(0, i - ZBT_MAX_DAYS):i]
            if len(window) and np.nanmin(window) <= ZBT_LOW:
                events.append(idx[i])
    return events


# ---------------------------------------------------------------------------
# Full recompute for a universe (bars store → persisted series)
# ---------------------------------------------------------------------------

def compute_universe(universe_key):
    """Rebuild breadth_daily + indicator_values for a universe from stored
    bars. Cheap relative to fetching (seconds), so recomputed wholesale —
    EMA/cumulative chains can't be incrementally appended safely anyway."""
    from . import store

    members = store.get_members(universe_key)
    if not members:
        return 0
    close, volume = store.get_panels(members)
    if close.empty:
        return 0
    agg = daily_aggregates(close, volume)
    if agg.empty:
        return 0
    der = derive(agg)
    store.replace_breadth_daily(universe_key, agg)
    store.replace_indicator_values(universe_key, der)
    return len(agg)
