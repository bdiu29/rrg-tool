"""
Bull/bear flag detection core — pure numpy, no I/O, no signal import.

This is the leaf the rest of the app shares so nothing has to re-implement (or
circularly import) the flag logic:
  * `flag_backtest.py` re-exports `_detect`/`_summary` and adds the yfinance
    download + CLI study (incl. the regime-conditioned variant).
  * `signal.py` calls `win_rates` to weight the flag factor in the conviction
    engine by its measured edge (per-symbol where available, else basket default).
  * `screener/metrics.py` mirrors the same geometry constants in a vectorized
    panel form (a unit test pins the two together so they can't drift).

A flag = an impulsive **flagpole** (a strong move over POLE_BARS) followed by a
brief, shallow consolidation on **tapering volume** (the flag). Detection is
no-lookahead at each flag (uses only bars up to the flag); the forward returns
it scores are, of course, in the future — that's the study.
"""

import numpy as np
import pandas as pd

# --- pattern parameters (daily bars) — single source of truth -------------
POLE_BARS     = 10     # window for the flagpole
FLAG_BARS     = 5      # window for the flag consolidation
POLE_MIN_RET  = 0.06   # flagpole must move ≥ this (fraction) over POLE_BARS
FLAG_MAX_RETR = 0.45   # flag may pull back at most this fraction of the pole
FLAG_MAX_RANGE = 0.5   # flag close-range ≤ this fraction of the pole (tightness)
FWD_HORIZONS  = (5, 10, 20)
SUCCESS_H     = 10     # horizon the headline success rate is measured at
COOLDOWN      = FLAG_BARS   # bars to wait before logging another flag on a symbol


def _detect(close, vol, require_taper):
    """Yield flag events for one symbol: (kind, index, {h: fwd_return})."""
    n = len(close)
    events = []
    last = -COOLDOWN - 1
    need = POLE_BARS + FLAG_BARS
    for e in range(need, n):
        if e - last < COOLDOWN:
            continue
        fs = e - FLAG_BARS + 1                       # flag start
        ps = fs - POLE_BARS                          # pole start
        pole_a, pole_b = close[ps], close[fs]        # pole endpoints
        if not (np.isfinite(pole_a) and pole_a > 0 and np.isfinite(pole_b)):
            continue
        pole_ret = (pole_b - pole_a) / pole_a
        pole_abs = abs(pole_b - pole_a)
        if pole_abs <= 0:
            continue
        flag = close[fs:e + 1]
        if not np.all(np.isfinite(flag)):
            continue
        flag_range = (np.max(flag) - np.min(flag)) / pole_abs
        # volume taper: mean volume in the flag below the pole's
        taper = True
        if require_taper:
            pv, fv = np.nanmean(vol[ps:fs + 1]), np.nanmean(vol[fs:e + 1])
            taper = np.isfinite(pv) and np.isfinite(fv) and pv > 0 and fv < pv

        kind = None
        if pole_ret >= POLE_MIN_RET:                 # bull flag
            retr = (pole_b - close[e]) / pole_abs     # pullback off the pole top
            if 0.0 <= retr <= FLAG_MAX_RETR and flag_range <= FLAG_MAX_RANGE and close[e] <= pole_b and taper:
                kind = "bull"
        elif pole_ret <= -POLE_MIN_RET:              # bear flag
            retr = (close[e] - pole_b) / pole_abs
            if 0.0 <= retr <= FLAG_MAX_RETR and flag_range <= FLAG_MAX_RANGE and close[e] >= pole_b and taper:
                kind = "bear"
        if kind is None:
            continue

        fwd = {}
        for h in FWD_HORIZONS:
            if e + h < n and np.isfinite(close[e + h]) and close[e] > 0:
                fwd[h] = (close[e + h] / close[e] - 1) * 100
        events.append((kind, e, fwd))
        last = e
    return events


def _summary(events, kind):
    """Stats for one kind ('bull'/'bear'). Success = move continued in the pole's
    direction (bull → up, bear → down) at SUCCESS_H."""
    rows = [ev for ev in events if ev[0] == kind]
    out = {"n": len(rows), "horizons": {}}
    for h in FWD_HORIZONS:
        r = np.array([ev[2][h] for ev in rows if h in ev[2]], dtype=float)
        if not r.size:
            continue
        cont = (r > 0) if kind == "bull" else (r < 0)     # continuation in pole direction
        out["horizons"][h] = {
            "n": int(r.size),
            "success_rate": round(100.0 * cont.mean(), 1),
            "avg_move": round(float(r.mean()), 2),
            "median_move": round(float(np.median(r)), 2),
        }
    return out


def flag_panels(close, volume, require_taper=True):
    """Vectorized per-bar flag state for a date×symbol panel → a `flag` panel of
    "bull"/"bear"/"none" (NaN before enough history). Same geometry/constants as the
    scalar `_detect`, expressed for pandas so the screener can scan it; no cooldown
    (it answers "is THIS bar a completed flag?"). No-lookahead: every input is the
    current bar or a trailing shift. `test_screener_metrics` pins this to `_detect`.

    A bar e is the flag's last bar; the flagpole ran from bar e−(FLAG_BARS−1)−POLE_BARS
    (`pole_a`) to e−(FLAG_BARS−1) (`pole_b`); the flag is the last FLAG_BARS closes."""
    s = FLAG_BARS - 1
    pole_b = close.shift(s)
    pole_a = close.shift(s + POLE_BARS)
    pole_ret = (pole_b - pole_a) / pole_a
    pole_abs = (pole_b - pole_a).abs()

    flag_max = close.rolling(FLAG_BARS, min_periods=FLAG_BARS).max()
    flag_min = close.rolling(FLAG_BARS, min_periods=FLAG_BARS).min()
    flag_range = (flag_max - flag_min) / pole_abs

    if require_taper:
        pole_vol = volume.rolling(POLE_BARS + 1, min_periods=POLE_BARS + 1).mean().shift(s)
        flag_vol = volume.rolling(FLAG_BARS, min_periods=FLAG_BARS).mean()
        taper = (pole_vol > 0) & (flag_vol < pole_vol)
    else:
        taper = pole_b.notna()                      # always true where defined

    base = (pole_abs > 0) & (flag_range <= FLAG_MAX_RANGE) & taper
    retr_bull = (pole_b - close) / pole_abs
    retr_bear = (close - pole_b) / pole_abs
    bull = base & (pole_ret >= POLE_MIN_RET) & (retr_bull >= 0) & (retr_bull <= FLAG_MAX_RETR) & (close <= pole_b)
    bear = base & (pole_ret <= -POLE_MIN_RET) & (retr_bear >= 0) & (retr_bear <= FLAG_MAX_RETR) & (close >= pole_b)

    out = pd.DataFrame("none", index=close.index, columns=close.columns, dtype=object)
    out = out.where(~bull.fillna(False), "bull")
    out = out.where(~bear.fillna(False), "bear")
    valid = pole_a.notna() & pole_b.notna() & flag_max.notna()    # window fully formed
    return out.where(valid)


def _regime_ok(kind, label):
    """A flag's empirical edge only counts in a regime that supports it: bear
    flags during DETERIORATING, bull flags otherwise (HEALTHY / NEUTRAL). An
    unknown label (None / "") never filters anything out."""
    if label is None or label == "":
        return True
    if kind == "bear":
        return label == "DETERIORATING"
    return label != "DETERIORATING"


def win_rates(close, vol, regime_labels=None, require_taper=True):
    """Per-symbol flag win-rate (continuation success at SUCCESS_H) for one
    symbol's own price+volume. Reuses `_detect`; tallies the same success rule
    as `_summary`. Returns {"bull": rate|None, "bull_n": int, "bear": rate|None,
    "bear_n": int} with rates in 0–1.

    `regime_labels`: optional per-bar array (aligned to `close`) of regime
    strings — bull events then count only outside DETERIORATING, bear events
    only inside it (the honest, regime-conditioned edge)."""
    close = np.asarray(close, dtype=float)
    vol = np.asarray(vol, dtype=float) if vol is not None else np.full(len(close), np.nan)
    events = _detect(close, vol, require_taper)
    out = {"bull": None, "bull_n": 0, "bear": None, "bear_n": 0}
    for kind in ("bull", "bear"):
        wins = total = 0
        for ev_kind, e, fwd in events:
            if ev_kind != kind or SUCCESS_H not in fwd:
                continue
            if regime_labels is not None and not _regime_ok(kind, regime_labels[e]):
                continue
            r = fwd[SUCCESS_H]
            total += 1
            if (r > 0) if kind == "bull" else (r < 0):
                wins += 1
        out[kind + "_n"] = total
        out[kind] = (wins / total) if total else None
    return out
