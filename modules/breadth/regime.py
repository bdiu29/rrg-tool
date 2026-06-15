"""
Regime filter + divergence detection + short-term signal interpretation —
the synthesis layer that makes the tracker more than a pile of charts.

Long-term breadth (Summation Index, % above 200d MA, divergence flags) sets a
regime: HEALTHY / NEUTRAL / DETERIORATING. Short-term extremes (McClellan,
TRIN, % above 20d) are then read THROUGH that regime — the same oversold
print is a buy-the-dip setup in HEALTHY and a fade-the-bounce in
DETERIORATING.
"""

import numpy as np
import pandas as pd

# --- tunable thresholds ----------------------------------------------------

SUM_SLOPE_DAYS = 5      # summation slope lookback
PCT200_BROAD   = 60.0   # % above 200d MA: broad/healthy above this
PCT200_NARROW  = 50.0   # narrow/deteriorating below this

DIV_LOOKBACK   = 63     # "new high" = highest close of ~a quarter
DIV_MIN_GAP    = 10     # bars between the highs being compared (and dedupe)
DIV_ACTIVE_BARS = 21    # a flag stays "active" for ~a month

# Short-term extremes (McClellan is ratio-adjusted → universe-independent scale)
MCC_OVERSOLD   = -70.0
MCC_OVERBOUGHT = 70.0
TRIN_PANIC     = 2.0
TRIN_EUPHORIA  = 0.5
PCT20_WASHOUT  = 20.0
PCT20_STRETCH  = 85.0
ZBT_RECENT_BARS = 10


# ---------------------------------------------------------------------------
# Divergence detection — discrete dated events, not just lines on a chart
# ---------------------------------------------------------------------------

def divergences(index_close, measures, lookback=DIV_LOOKBACK, min_gap=DIV_MIN_GAP):
    """index_close: Series. measures: {name: Series} of breadth lines.

    Bearish: index makes a fresh `lookback`-day high above its prior high,
    but the breadth measure sits below its own value at that prior high.
    Bullish is the mirror at lows. Returns [{date, kind, measure, detail}].
    """
    index_close = index_close.dropna()
    if len(index_close) < lookback + min_gap:
        return []
    iv       = index_close.to_numpy(dtype=float)
    dates    = index_close.index
    is_high  = (index_close >= index_close.rolling(lookback, min_periods=lookback).max()).to_numpy()
    is_low   = (index_close <= index_close.rolling(lookback, min_periods=lookback).min()).to_numpy()

    events = []
    for name, m in measures.items():
        mv = m.reindex(index_close.index).to_numpy(dtype=float)
        for kind, marks, idx_cmp, m_cmp in (
            ("bearish", np.where(is_high)[0], np.greater, np.less),
            ("bullish", np.where(is_low)[0],  np.less,    np.greater),
        ):
            last_emit = None
            for i in marks:
                prior = marks[marks <= i - min_gap]
                if len(prior) == 0:
                    continue
                j = prior[-1]
                if np.isnan(mv[i]) or np.isnan(mv[j]):
                    continue
                if idx_cmp(iv[i], iv[j]) and m_cmp(mv[i], mv[j]):
                    if last_emit is not None and i - last_emit < min_gap:
                        continue
                    word = "high" if kind == "bearish" else "low"
                    events.append({
                        "date":    str(dates[i]),
                        "kind":    kind,
                        "measure": name,
                        "detail":  f"index new {lookback}d {word} not confirmed by {name}",
                    })
                    last_emit = i
    events.sort(key=lambda e: e["date"])
    return events


def active_divergences(events, all_dates, window=DIV_ACTIVE_BARS):
    """Events recent enough (within `window` bars of the last date) to still
    color the current regime."""
    if not events or len(all_dates) == 0:
        return []
    recent = set(str(d) for d in all_dates[-window:])
    return [e for e in events if e["date"] in recent]


# ---------------------------------------------------------------------------
# Regime state
# ---------------------------------------------------------------------------

def regime_state(summation, pct200, active_divergences=0):
    """Score the long-term panel. HEALTHY demands everything green
    (positive + rising summation AND broad %>200d); one bad leg drops to
    NEUTRAL; broad weakness or divergence stacking reads DETERIORATING."""
    summation = summation.dropna()
    pct200    = pct200.dropna()
    score, reasons = 0, []

    s_last = float(summation.iloc[-1])
    slope_n = min(SUM_SLOPE_DAYS, len(summation) - 1)
    s_slope = s_last - float(summation.iloc[-1 - slope_n]) if slope_n > 0 else 0.0
    if s_last > 0:
        score += 1
        reasons.append(f"Summation Index positive ({s_last:+.0f})")
    else:
        score -= 1
        reasons.append(f"Summation Index negative ({s_last:+.0f})")
    if s_slope > 0:
        score += 1
        reasons.append(f"Summation rising ({s_slope:+.0f} over {slope_n}d)")
    else:
        score -= 1
        reasons.append(f"Summation falling ({s_slope:+.0f} over {slope_n}d)")

    p_last = float(pct200.iloc[-1]) if len(pct200) else float("nan")
    if not np.isnan(p_last):
        if p_last >= PCT200_BROAD:
            score += 1
            reasons.append(f"{p_last:.0f}% of stocks above 200d MA — broad participation")
        elif p_last < PCT200_NARROW:
            score -= 1
            reasons.append(f"only {p_last:.0f}% of stocks above 200d MA — narrow market")
        else:
            reasons.append(f"{p_last:.0f}% of stocks above 200d MA — middling")

    if active_divergences:
        score -= min(int(active_divergences), 2)
        reasons.append(f"{active_divergences} active divergence flag(s)")

    if score >= 3:
        state = "HEALTHY"
    elif score <= -2:
        state = "DETERIORATING"
    else:
        state = "NEUTRAL"
    return {"state": state, "score": score, "reasons": reasons}


def regime_series(summation, pct200):
    """Vectorized per-date regime label (HEALTHY / NEUTRAL / DETERIORATING) — the
    historical counterpart of `regime_state`, for studies that need the regime at
    every bar (e.g. conditioning the flag win-rate, the conviction regime gate).

    Approximation vs `regime_state`: scores the summation-sign + 5d-slope + %>200d
    legs only and drops the discrete divergence leg (which only ever subtracts), so
    it is conservative — it labels DETERIORATING slightly less often than the live
    point-in-time state. Same thresholds, so it agrees with `regime_state` whenever
    no divergence flag is active. Returns a Series aligned to `summation.index`."""
    s = summation.astype(float)
    p = pct200.reindex(s.index).astype(float)

    s_sign = np.where(s > 0, 1, -1)
    slope  = s.diff(SUM_SLOPE_DAYS).fillna(0.0)         # early bars: 0 slope → falling leg
    sl_sign = np.where(slope > 0, 1, -1)
    p_leg  = np.where(p >= PCT200_BROAD, 1,
                      np.where(p < PCT200_NARROW, -1, 0))
    p_leg  = np.where(np.isnan(p), 0, p_leg)            # unknown %>200d contributes nothing

    score = s_sign + sl_sign + p_leg
    label = np.where(score >= 3, "HEALTHY",
                     np.where(score <= -2, "DETERIORATING", "NEUTRAL"))
    return pd.Series(label, index=s.index)


def align_labels(labels, target_index):
    """Reindex a regime-label Series (breadth dates are 'YYYY-MM-DD' strings) onto
    `target_index`, which may be a DatetimeIndex (the yfinance/RRG path) OR string
    dates (the breadth bars store) — comparing the two raw would silently align to
    all-NaN. We normalize both to 'YYYY-MM-DD' for the join, then restore the
    caller's index and forward-fill gaps. Returns a Series indexed by target_index,
    or None if `labels` is None."""
    if labels is None:
        return None
    target = pd.Index(target_index)
    keys = pd.to_datetime(target).strftime("%Y-%m-%d")
    out = labels.reindex(keys)
    out.index = target
    with pd.option_context("future.no_silent_downcasting", True):
        return out.ffill()


# ---------------------------------------------------------------------------
# Short-term signal interpretation, conditioned on the regime
# ---------------------------------------------------------------------------

_DIP_READ = {
    "HEALTHY":       "buy-the-dip setup — regime supports it, can size up",
    "NEUTRAL":       "constructive, but wait for confirmation before sizing up",
    "DETERIORATING": "lower confidence — bias toward fading the bounce, smaller size, tighter stops",
}

_RALLY_READ = {
    "HEALTHY":       "momentum can carry — avoid chasing, trail stops",
    "NEUTRAL":       "stretch developing — tighten risk on longs",
    "DETERIORATING": "rally into a deteriorating regime — fade candidate",
}


def interpret(state, latest, zbt_recent=None):
    """latest: dict with mcclellan, trin, pct_above_20 (NaNs ok).
    Returns interpretation lines for the dashboard / daily summary."""
    lines = []
    mcc   = latest.get("mcclellan")
    trin  = latest.get("trin")
    pct20 = latest.get("pct_above_20")

    if mcc is not None and not np.isnan(mcc):
        if mcc <= MCC_OVERSOLD:
            lines.append(f"McClellan oversold ({mcc:.0f}): {_DIP_READ[state]}.")
        elif mcc >= MCC_OVERBOUGHT:
            lines.append(f"McClellan overbought ({mcc:+.0f}): {_RALLY_READ[state]}.")
    if trin is not None and not np.isnan(trin):
        if trin >= TRIN_PANIC:
            lines.append(f"TRIN {trin:.2f} — panic-selling washout; contrarian bounce zone.")
        elif trin <= TRIN_EUPHORIA:
            lines.append(f"TRIN {trin:.2f} — one-sided buying; short-term exhaustion risk.")
    if pct20 is not None and not np.isnan(pct20):
        if pct20 <= PCT20_WASHOUT:
            lines.append(f"Only {pct20:.0f}% above 20d MA — washed out; {_DIP_READ[state]}.")
        elif pct20 >= PCT20_STRETCH:
            lines.append(f"{pct20:.0f}% above 20d MA — stretched; {_RALLY_READ[state]}.")
    if zbt_recent:
        lines.append(f"Zweig Breadth Thrust fired {zbt_recent} — rare bullish momentum signal.")
    if not lines:
        lines.append("No short-term extremes — follow the regime bias.")
    return lines
