"""
Pure pandas snapshot math for the screener — no I/O, unit-tested.

compute_snapshot() turns date×symbol OHLCV panels (from the breadth bars
store) into one indicator row per symbol. All windows are trailing; the
rolling high/low *levels* are shifted one day so a cross by today's (or a
live intraday) price is detectable against yesterday's level.
"""

import numpy as np
import pandas as pd

# Flag + volume-exhaustion detection are owned by the confluence leaves (single
# source of truth, shared with the conviction engine and the flag study); we reuse
# the vectorized panel forms here rather than re-implementing them. No new I/O.
from modules.confluence import flags as rrg_flags
from modules.confluence import exhaustion as rrg_exhaustion

# ≈320 trading bars: 252 for 52-week levels + warmup buffer for SMA200/RSI.
SNAPSHOT_LOOKBACK_DAYS = 470

SESSION_MINUTES = 390          # 9:30–16:00 ET
MIN_SESSION_FRACTION = 0.02    # floor stops open-bell RVOL blowups

SMA_SPANS = (20, 50, 150, 200)
EMA_SPANS = (5, 10, 20, 50, 100, 200)

# Golden pocket (Fibonacci) — retracement of the most recent swing leg.
GP_PIVOT          = 5      # bars on each side of a swing pivot (fractal width)
GP_LOW            = 0.618  # golden-pocket band, lower retracement bound
GP_HIGH           = 0.786  # golden-pocket band, upper retracement bound
GP_APPROACH_FLOOR = 0.5    # price "approaching" the pocket from just below 0.618


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


def golden_pocket(high, low, close, pivot=GP_PIVOT):
    """Fibonacci golden-pocket panels for the most recent swing leg.

    A bar is a swing high/low when its high/low is *strictly* above/below every
    bar in the `pivot` bars on each side (strictness ignores flat runs, which
    would otherwise read as both a high and a low). Such a pivot is only
    *confirmed* `pivot` bars later (it needs the future bars), so the confirmed
    pivot price/position is shifted forward by `pivot` before being carried
    forward — no lookahead at any evaluation date.

    The active leg is the more recent of the last swing high and last swing
    low. Most-recent-pivot is a high ⇒ *bullish* leg (low→high, price retracing
    down); a low ⇒ *bearish* leg (high→low, price retracing up). Returns
    date×symbol panels: gp_direction, gp_retrace (0–1 of the leg), gp_in_pocket
    / gp_approaching (1.0/0.0 flags), gp_zone_low / gp_zone_high (pocket prices).
    """
    left_max  = high.rolling(pivot).max().shift(1)       # pivot bars before i
    right_max = high.rolling(pivot).max().shift(-pivot)  # pivot bars after i
    left_min  = low.rolling(pivot).min().shift(1)
    right_min = low.rolling(pivot).min().shift(-pivot)
    sh = (high > left_max) & (high > right_max)
    sl = (low  < left_min) & (low  < right_min)

    pos = pd.DataFrame(
        np.repeat(np.arange(len(close)).reshape(-1, 1), close.shape[1], axis=1),
        index=close.index, columns=close.columns, dtype=float)

    hi_price = high.where(sh).shift(pivot).ffill()
    lo_price = low.where(sl).shift(pivot).ffill()
    hi_pos   = pos.where(sh).shift(pivot).ffill()
    lo_pos   = pos.where(sl).shift(pivot).ffill()

    rng     = hi_price - lo_price
    valid   = (rng > 0) & hi_pos.notna() & lo_pos.notna()
    bullish = hi_pos > lo_pos          # most recent pivot is a swing high

    retr_bull = (hi_price - close) / rng     # 0 at the high, 1 at the low
    retr_bear = (close - lo_price) / rng     # 0 at the low, 1 at the high
    retrace   = retr_bull.where(bullish, retr_bear).where(valid)
    notna     = retrace.notna()

    direction = pd.DataFrame(np.where(bullish.to_numpy(), "bullish", "bearish"),
                             index=close.index, columns=close.columns).where(valid)
    in_pocket   = ((retrace >= GP_LOW) & (retrace <= GP_HIGH)).astype(float).where(notna)
    approaching = ((retrace >= GP_APPROACH_FLOOR) & (retrace < GP_LOW)).astype(float).where(notna)

    # Pocket as price levels (low/high band), oriented by leg direction.
    zone_a = hi_price - GP_LOW  * rng        # bullish: shallow edge (0.618)
    zone_b = hi_price - GP_HIGH * rng        # bullish: deep edge (0.786)
    zone_c = lo_price + GP_LOW  * rng        # bearish edges
    zone_d = lo_price + GP_HIGH * rng
    zone_low  = np.minimum(zone_a, zone_b).where(bullish, np.minimum(zone_c, zone_d)).where(valid)
    zone_high = np.maximum(zone_a, zone_b).where(bullish, np.maximum(zone_c, zone_d)).where(valid)

    return {
        "gp_direction": direction, "gp_retrace": retrace,
        "gp_in_pocket": in_pocket, "gp_approaching": approaching,
        "gp_zone_low": zone_low, "gp_zone_high": zone_high,
    }


def compute_indicator_panels(close, volume, open_, high, low, spy_close):
    """→ dict of date×symbol indicator panels (full trailing history).

    Single source of truth for the indicator math. `compute_snapshot` collapses
    this to the last row per symbol; the backtester keeps the whole history and
    slices a cross-section per rebalance date. Panels: date×symbol, ascending
    YYYY-MM-DD index. spy_close: Series of SPY closes (aligned/ffilled here).
    """
    if close.empty:
        return {}

    chg_pct     = close.pct_change(fill_method=None) * 100
    gap_pct     = (open_ / close.shift(1) - 1) * 100
    vol_chg_pct = volume.pct_change(fill_method=None) * 100

    # avg includes the last completed bar (the right base for the NEXT live
    # day); the stored EOD rvol divides by the average of the 10 days BEFORE
    # the snapshot bar so the spike day doesn't dampen its own ratio.
    avg_vol_10d  = volume.rolling(10, min_periods=10).mean()
    rvol_10d     = volume / avg_vol_10d.shift(1)

    smas = {n: close.rolling(n, min_periods=n).mean() for n in SMA_SPANS}
    emas = {n: close.ewm(span=n, adjust=False, min_periods=n).mean() for n in EMA_SPANS}

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

    panels = {
        "close": close, "open": open_, "volume": volume,
        "chg_pct": chg_pct, "gap_pct": gap_pct, "vol_chg_pct": vol_chg_pct,
        "avg_vol_10d": avg_vol_10d, "rvol_10d": rvol_10d,
        "rsi14": rsi14, "atr14": atr14, "atr_pct": atr_pct,
        "rs_1m_pct": rs_parts["rs_1m_pct"], "rs_3m_pct": rs_parts["rs_3m_pct"],
        "high_20d": high_20d, "low_20d": low_20d,
        "high_252": high_252, "low_252": low_252,
        "pct_from_52w_high": pct_from_52w_high,
        "pct_from_52w_low": pct_from_52w_low,
    }
    panels.update({f"sma{n}": smas[n] for n in SMA_SPANS})
    panels.update({f"ema{n}": emas[n] for n in EMA_SPANS})
    panels.update(golden_pocket(high, low, close))
    # bull/bear flag state + volume buyer/seller exhaustion (rrg leaves)
    panels["flag"] = rrg_flags.flag_panels(close, volume)
    panels["exhaustion"] = rrg_exhaustion.exhaustion_panels(high, low, close, volume)
    return panels


def compute_snapshot(close, volume, open_, high, low, spy_close):
    """→ DataFrame, one row per symbol (SNAPSHOT_COLS minus what store adds).

    Symbols whose last bar is older than the panel's newest date come out
    mostly NaN — stale names never match a filter, by design.
    """
    panels = compute_indicator_panels(close, volume, open_, high, low, spy_close)
    if not panels:
        return pd.DataFrame()
    out = pd.DataFrame({k: v.iloc[-1] for k, v in panels.items()})
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
