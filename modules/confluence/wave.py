"""
Wave engine — the cohesive Elliott-wave / Fibonacci / ZigZag / RSI-divergence / ABC
analysis on a security's raw RS line, extracted verbatim from `rrg.signal` into the
confluence layer so every module can read wave structure without importing rrg.

This is NOT a uniform single-factor leaf like flags/exhaustion/volume_profile — the
pieces (significant-swing ZigZag → wave labels, golden-pocket vs shallow retrace
depth, wave-3/5 Fib extensions, the ABC corrective family, dual RSI divergence) share
ONE no-lookahead ZigZag pass in `_wave_features`, so they travel together. The fetch/
orchestration that feeds it prices, the multi-timeframe blend, and the conviction
combiner stay in `rrg.signal` (its decision policy); this module is pure math.

`_wave_features(rs_s, params, price)` reads the swing sensitivity `ZIGZAG_K` from the
threaded params dict (the only searchable wave param), falling back to the default
here — which `rrg.signal.DEFAULTS` merges so the live + backtest paths share one value.
"""

import numpy as np
import pandas as pd

# ZIGZAG_K is the wave engine's one searchable param; this is its single-source
# default (rrg.signal.DEFAULTS merges it, so re-baking it here updates both the live
# chart and the backtest grid).
_WAVE_DEFAULTS = {"ZIGZAG_K": 1.25}


def _p(params, key):
    return (params or {}).get(key, _WAVE_DEFAULTS[key])


ZIGZAG_WIN  = 26    # rolling-σ window (bars) for the volatility-scaled swing threshold
RSI_N       = 14    # Wilder RSI lookback for the divergence layer (standard, not searched)

# Fibonacci structure constants — theory-fixed, NOT searched (tuning these would
# curve-fit Elliott theory to 11 ETFs). Retracements/extensions are measured on the
# raw RS line, where distance is proportional to the real relative move; the JdK
# RS-Ratio oscillation is display-only.
GP_LOW, GP_HIGH        = 0.618, 0.786   # wave-2 golden-pocket retracement band (highest-odds zone)
GP_APPROACH_FLOOR      = 0.50           # "approaching" the golden pocket from below
WAVE2_MAX              = 0.99           # wave 2 may retrace up to ~99% of wave 1; ≥1.0 invalidates
SHALLOW_LO, SHALLOW_HI = 0.236, 0.382   # wave-4 shallow retracement band
W3_WARN_RATIO          = 2.618          # warn as wave 3 nears this ×w1 (1.618 is the expected target)

# Flag pattern: a strong impulse leg (the flagpole) followed by a brief, shallow,
# tight consolidation against it (the flag) → continuation in the pole's direction.
FLAG_RETR_MAX = 0.45   # the flag may retrace at most this much of the pole
FLAG_DUR_MAX  = 0.75   # …over at most this fraction of the pole's duration


def _zigzag_swings(series, k, win=ZIGZAG_WIN):
    """Volatility-scaled ZigZag of *significant* swings. A running extreme becomes
    a confirmed pivot only when the line reverses from it by ≥ `k × rolling σ` of
    the series — so "significant" adapts to each timeframe's volatility (no fixed
    bar-count window to tune). The pivot is dated at the extreme (`pos`) but
    confirmed at the reversal bar (`confirm`), so it is only known as-of `confirm`
    (no lookahead). Inherently alternating H/L. Returns {confirm, pos, price, kind}
    in confirm order — list[:j] over confirms ≤ e is exactly the as-of-bar-e set."""
    v   = series.to_numpy(dtype=float)
    n   = len(v)
    thr = (series.rolling(win, min_periods=max(5, win // 2)).std(ddof=0)
           * k).to_numpy()
    piv = []
    trend = 0                          # 0 unknown, +1 up-leg, -1 down-leg
    hi_p = lo_p = None
    hi_pos = lo_pos = 0
    for i in range(n):
        x, t = v[i], thr[i]
        if not np.isfinite(x):
            continue
        if hi_p is None:
            hi_p = lo_p = x; hi_pos = lo_pos = i
            continue
        if not np.isfinite(t) or t <= 0:
            t = np.inf                     # before σ exists: track extremes, confirm nothing
        if trend >= 0:                     # up-leg (or undecided): track the high
            if x > hi_p:
                hi_p, hi_pos = x, i
            if x <= hi_p - t:              # reversed down ≥ threshold → confirm the High
                piv.append({"confirm": i, "pos": hi_pos, "price": hi_p, "kind": "H"})
                trend, lo_p, lo_pos = -1, x, i
                continue
        if trend <= 0:                     # down-leg (or undecided): track the low
            if x < lo_p:
                lo_p, lo_pos = x, i
            if x >= lo_p + t:             # reversed up ≥ threshold → confirm the Low
                piv.append({"confirm": i, "pos": lo_pos, "price": lo_p, "kind": "L"})
                trend, hi_p, hi_pos = 1, x, i
    return piv


def _rsi(series, n=RSI_N):
    """Wilder RSI on a Series. Ported from the screener's metrics.rsi so RRG (the
    lowest module) stays self-contained — it must NOT import upward."""
    delta = series.diff()
    up    = delta.clip(lower=0)
    down  = (-delta).clip(lower=0)
    au = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    ad = down.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = au / ad
    return (100 - 100 / (1 + rs)).where(~np.isinf(rs), 100.0)


def _divergence(vis, x, rsi, e):
    """RSI divergence at bar `e` (price value `x`). The *forming* read — the live
    bar pushing past the last confirmed pivot while RSI lags — is the early,
    project-ahead signal (don't wait for the second pivot to confirm); it falls
    back to the *confirmed* read off the last two same-kind pivots. `rsi` is
    whichever array (RS line or price). Returns 'bull' / 'bear' / 'none'.

    No-lookahead: `rsi[e]` is causal and `rsi[pivot.pos]` is known by the pivot's
    confirmation bar (pos+width ≤ e), so this only reads as-of-`e` information."""
    if not (0 <= e < len(rsi)) or not np.isfinite(rsi[e]):
        return "none"
    Hs = [p for p in vis if p["kind"] == "H"]
    Ls = [p for p in vis if p["kind"] == "L"]
    # forming: price beyond the last pivot, momentum failing to confirm
    if Hs and np.isfinite(rsi[Hs[-1]["pos"]]) and x > Hs[-1]["price"] and rsi[e] < rsi[Hs[-1]["pos"]]:
        return "bear"
    if Ls and np.isfinite(rsi[Ls[-1]["pos"]]) and x < Ls[-1]["price"] and rsi[e] > rsi[Ls[-1]["pos"]]:
        return "bull"
    # confirmed: last two same-kind pivots
    if (len(Hs) >= 2 and np.isfinite(rsi[Hs[-1]["pos"]]) and np.isfinite(rsi[Hs[-2]["pos"]])
            and Hs[-1]["price"] > Hs[-2]["price"] and rsi[Hs[-1]["pos"]] < rsi[Hs[-2]["pos"]]):
        return "bear"
    if (len(Ls) >= 2 and np.isfinite(rsi[Ls[-1]["pos"]]) and np.isfinite(rsi[Ls[-2]["pos"]])
            and Ls[-1]["price"] < Ls[-2]["price"] and rsi[Ls[-1]["pos"]] > rsi[Ls[-2]["pos"]]):
        return "bull"
    return "none"


def _last_of(vis, kind, before_pos=None):
    for p in reversed(vis):
        if p["kind"] == kind and (before_pos is None or p["pos"] < before_pos):
            return p
    return None


def _clamp01(x):
    return 0.0 if not np.isfinite(x) else max(0.0, min(1.0, float(x)))


def _gp_quality(retrace):
    """Golden-pocket depth quality [0,1] for a wave-2 retracement: 1.0 inside the
    0.618–0.786 pocket (highest odds), ramping up from the 0.5 floor and tapering
    to 0 at the ~0.99 wave-2 max. Lets a partial-depth pullback contribute partial
    conviction instead of being all-or-nothing."""
    if not np.isfinite(retrace):
        return 0.0
    if GP_LOW <= retrace <= GP_HIGH:
        return 1.0
    if GP_APPROACH_FLOOR <= retrace < GP_LOW:
        return _clamp01((retrace - GP_APPROACH_FLOOR) / (GP_LOW - GP_APPROACH_FLOOR))
    if GP_HIGH < retrace <= WAVE2_MAX:
        return _clamp01((WAVE2_MAX - retrace) / (WAVE2_MAX - GP_HIGH))
    return 0.0


def _shallow_quality(retrace):
    """Wave-4 shallow-retracement quality [0,1], peaking mid-band (~0.31)."""
    if not np.isfinite(retrace) or not (SHALLOW_LO <= retrace <= SHALLOW_HI):
        return 0.0
    mid, half = (SHALLOW_LO + SHALLOW_HI) / 2, (SHALLOW_HI - SHALLOW_LO) / 2
    return _clamp01(1.0 - abs(retrace - mid) / half * 0.4)


_EMPTY_WAVE = {"trend": "none", "leg_kind": "none", "wave": "—", "retrace": np.nan,
               "ext": np.nan, "fib_target": np.nan, "fib_w1": np.nan,
               "gp_q": 0.0, "ext_q": 0.0}


def _classify_wave(vis, x):
    """Label the head bar from the visible (as-of) ZigZag pivots `vis` (position
    ordered) and the current RS value `x`. Depth is the classifier: a deep
    pullback (≥0.5, golden pocket) is wave-2, a shallow one (0.236–0.382) is
    wave-4; the preceding correction's depth labels the current impulse wave-3
    (after a deep w2) vs wave-5 (after a shallow w4). Mirror for downtrends."""
    if len(vis) < 2 or not np.isfinite(x):
        return dict(_EMPTY_WAVE)
    highs = [p for p in vis if p["kind"] == "H"]
    lows  = [p for p in vis if p["kind"] == "L"]
    if not highs or not lows:
        return dict(_EMPTY_WAVE)

    out = dict(_EMPTY_WAVE)
    # Trend from whatever same-kind pivot pairs exist: higher-high / higher-low =
    # up, lower = down. With ≥2 highs and ≥2 lows this is the robust HH/HL read;
    # with only one pair (e.g. [L,H,L]) the available comparison still resolves it,
    # so a developing wave-1 → wave-2 registers before a full uptrend is provable.
    hh = highs[-1]["price"] > highs[-2]["price"] if len(highs) >= 2 else None
    hl = lows[-1]["price"]  > lows[-2]["price"]  if len(lows)  >= 2 else None
    signals = [v for v in (hh, hl) if v is not None]
    if signals:
        if all(signals):
            out["trend"] = "up"
        elif not any(signals):
            out["trend"] = "down"
        else:
            out["trend"] = "ambiguous"
            return out
    else:
        # Only one pivot of each kind — bias from the last completed leg.
        a, b = vis[-2], vis[-1]
        if a["kind"] == "L" and b["kind"] == "H":
            out["trend"] = "up"
        elif a["kind"] == "H" and b["kind"] == "L":
            out["trend"] = "down"
        else:
            return out

    p_last = vis[-1]
    if out["trend"] == "up":
        if p_last["kind"] == "H" and x < p_last["price"]:           # corrective pullback
            A_high, A_low = p_last["price"], lows[-1]["price"]
            imp = A_high - A_low
            if imp <= 0:
                return out
            retrace = (A_high - x) / imp
            out["leg_kind"], out["retrace"] = "corrective", retrace
            if SHALLOW_LO <= retrace <= SHALLOW_HI:
                out["wave"], out["gp_q"] = "wave-4", _shallow_quality(retrace)
            elif GP_APPROACH_FLOOR <= retrace <= WAVE2_MAX:    # wave 2 valid up to ~99%
                out["wave"], out["gp_q"] = "wave-2", _gp_quality(retrace)
            return out
        if x > lows[-1]["price"]:                                   # impulse up
            launch = lows[-1]
            Hc = _last_of(vis, "H", launch["pos"])
            Lp = _last_of(vis, "L", Hc["pos"]) if Hc else None
            if Hc is None or Lp is None:
                return out
            prior_imp = Hc["price"] - Lp["price"]
            if prior_imp <= 0:
                return out
            corr_depth = (Hc["price"] - launch["price"]) / prior_imp
            out["leg_kind"] = "impulse"
            if corr_depth >= GP_LOW:                                # deep w2 → wave 3
                w1 = prior_imp
                out["wave"], out["fib_w1"] = "wave-3", w1
                out["ext"] = (x - launch["price"]) / w1
                out["fib_target"] = launch["price"] + W3_WARN_RATIO * w1
                out["ext_q"] = _clamp01((out["ext"] - 1.618) / (W3_WARN_RATIO - 1.618))
            elif corr_depth <= SHALLOW_HI:                          # shallow w4 → wave 5
                H1 = _last_of(vis, "H", Lp["pos"])
                L0 = _last_of(vis, "L", H1["pos"]) if H1 else None
                if H1 is None or L0 is None:
                    return out
                w1, net13 = H1["price"] - L0["price"], Hc["price"] - L0["price"]
                if w1 <= 0:
                    return out
                out["wave"], out["fib_w1"] = "wave-5", w1
                out["ext"] = (x - launch["price"]) / w1
                tgts = [launch["price"] + m for m in
                        (1.0 * w1, 0.618 * w1, 0.618 * net13, 1.618 * net13)]
                unmet = [t for t in tgts if t >= x]
                out["fib_target"] = min(unmet) if unmet else max(tgts)
                out["ext_q"] = _clamp01(1.0 - (out["fib_target"] - x) / (0.5 * w1))
            return out
        return out

    # trend == "down" — mirror
    if p_last["kind"] == "L" and x > p_last["price"]:               # corrective bounce
        A_low, A_high = p_last["price"], highs[-1]["price"]
        imp = A_high - A_low
        if imp <= 0:
            return out
        retrace = (x - A_low) / imp
        out["leg_kind"], out["retrace"] = "corrective", retrace
        if SHALLOW_LO <= retrace <= SHALLOW_HI:
            out["wave"], out["gp_q"] = "wave-4", _shallow_quality(retrace)
        elif GP_APPROACH_FLOOR <= retrace <= WAVE2_MAX:
            out["wave"], out["gp_q"] = "wave-2", _gp_quality(retrace)
        return out
    if x < highs[-1]["price"]:                                      # impulse down
        launch = highs[-1]
        Lc = _last_of(vis, "L", launch["pos"])
        Hp = _last_of(vis, "H", Lc["pos"]) if Lc else None
        if Lc is None or Hp is None:
            return out
        prior_imp = Hp["price"] - Lc["price"]
        if prior_imp <= 0:
            return out
        corr_depth = (launch["price"] - Lc["price"]) / prior_imp
        out["leg_kind"] = "impulse"
        if corr_depth >= GP_LOW:
            w1 = prior_imp
            out["wave"], out["fib_w1"] = "wave-3", w1
            out["ext"] = (launch["price"] - x) / w1
            out["fib_target"] = launch["price"] - W3_WARN_RATIO * w1
            out["ext_q"] = _clamp01((out["ext"] - 1.618) / (W3_WARN_RATIO - 1.618))
        elif corr_depth <= SHALLOW_HI:
            L1 = _last_of(vis, "L", Hp["pos"])
            H0 = _last_of(vis, "H", L1["pos"]) if L1 else None
            if L1 is None or H0 is None:
                return out
            w1, net13 = H0["price"] - L1["price"], H0["price"] - Lc["price"]
            if w1 <= 0:
                return out
            out["wave"], out["fib_w1"] = "wave-5", w1
            out["ext"] = (launch["price"] - x) / w1
            tgts = [launch["price"] - m for m in
                    (1.0 * w1, 0.618 * w1, 0.618 * net13, 1.618 * net13)]
            unmet = [t for t in tgts if t <= x]
            out["fib_target"] = max(unmet) if unmet else min(tgts)
            out["ext_q"] = _clamp01(1.0 - (x - out["fib_target"]) / (0.5 * w1))
        return out
    return out


def _flag_signal(vis, v, e):
    """Bull/bear flag on the RS line. The flagpole is the strong run from the last
    confirmed pivot to the running extreme *since* that pivot (the flag's shallow
    pullback is, by definition, too small to confirm a new pivot — so the pole top
    is a running extreme, not a ZigZag pivot). The flag is a brief, shallow
    consolidation against the pole (retrace ≤ FLAG_RETR_MAX over ≤ FLAG_DUR_MAX of
    the pole's duration). Up pole + shallow pullback → bull flag (+1, continuation
    up); down pole + shallow bounce → bear flag (−1). Volume confirmation lives in
    the standalone flag backtest (the RS line has no volume)."""
    if not vis:
        return 0.0
    p = vis[-1]
    seg = v[p["pos"]:e + 1]
    if len(seg) < 4 or not np.all(np.isfinite(seg)):
        return 0.0
    x = v[e]
    if p["kind"] == "L":                                   # up pole + bull flag
        k = int(np.argmax(seg)); peak = seg[k]
        pole, pole_dur, flag_dur = peak - p["price"], k, (len(seg) - 1) - k
        if pole <= 0 or pole_dur < 3:
            return 0.0
        retr = (peak - x) / pole
        if 0.05 <= retr <= FLAG_RETR_MAX and 1 <= flag_dur <= FLAG_DUR_MAX * pole_dur and x < peak:
            return 1.0
    else:                                                  # down pole + bear flag
        k = int(np.argmin(seg)); trough = seg[k]
        pole, pole_dur, flag_dur = p["price"] - trough, k, (len(seg) - 1) - k
        if pole <= 0 or pole_dur < 3:
            return 0.0
        retr = (x - trough) / pole
        if 0.05 <= retr <= FLAG_RETR_MAX and 1 <= flag_dur <= FLAG_DUR_MAX * pole_dur and x > trough:
            return -1.0
    return 0.0


# ABC corrective family (theory-fixed). A five-wave advance is corrected by a
# three-wave A-B-C against the trend. Zigzag (sharp) vs flat (sideways) is told
# apart by how deeply B retraces A; the C leg then projects a target zone.
ABC_ZIGZAG_MAX_B = 0.618             # B retraces < this of A → zigzag; ≥ this → flat
ABC_C_LO, ABC_C_HI = 0.618, 1.618    # zigzag C target band (×A, projected down from B)
_EMPTY_ABC = {"abc_type": "—", "abc_leg": "—", "c_tgt_lo": np.nan,
              "c_tgt_hi": np.nan, "w4_zone": np.nan}


def _abc_state(vis, x):
    """Post-wave-5 ABC correction. The dominant peak (highest swing high) is the
    one being corrected; it triggers ABC only if its own leg was a **wave-5** (the
    correction preceding its launch low was *shallow* — the same test that splits
    wave-3 from wave-5). A wave-3 peak instead means a wave-4 continuation dip
    (Phase-1 territory), so this returns None there. Once ABC is active: 1st low
    after the top = end of A, next high = end of B (zigzag if B retraces <61.8% of
    A, else flat), then C projects down. The prior wave-4 low is the classic
    C-bottom confluence. Returns a feature dict or None."""
    if len(vis) < 4 or not np.isfinite(x):
        return None
    highs = [p for p in vis if p["kind"] == "H"]
    if not highs:
        return None
    top = max(highs, key=lambda p: p["price"])      # the dominant peak
    if x >= top["price"]:
        return None
    top_i = vis.index(top)

    # The top is a genuine wave-5 top only with the full alternation: wave-4
    # (Hc→launch) SHALLOW and the wave-2 before it (H1→Lp) DEEP. That stricter gate
    # (≥6 pivots) keeps ABC from firing on every peak that follows a shallow dip.
    launch = _last_of(vis[:top_i], "L")             # wave-4 low candidate
    if launch is None:
        return None
    Hc = _last_of(vis[:top_i], "H", launch["pos"])  # wave-3 top
    Lp = _last_of(vis[:top_i], "L", Hc["pos"]) if Hc else None   # wave-2 low
    if not (Hc and Lp) or (Hc["price"] - Lp["price"]) <= 0:
        return None
    if (Hc["price"] - launch["price"]) / (Hc["price"] - Lp["price"]) > SHALLOW_HI:
        return None                                  # wave-4 not shallow → top isn't a wave-5
    H1 = _last_of(vis[:vis.index(Lp)], "H")          # wave-1 top
    L0 = _last_of(vis[:vis.index(H1)], "L") if H1 else None   # wave-1 start
    if not (H1 and L0) or (H1["price"] - L0["price"]) <= 0:
        return None
    if (H1["price"] - Lp["price"]) / (H1["price"] - L0["price"]) < GP_LOW:
        return None                                  # wave-2 not deep → not a clean 5-wave advance

    out = dict(_EMPTY_ABC)
    out["w4_zone"] = launch["price"]
    out["abc_leg"] = "A"
    post = vis[top_i + 1:]                            # confirmed pivots after the top
    a_low = post[0] if post and post[0]["kind"] == "L" else None
    if a_low is None:
        return out                                   # wave A still falling, no confirmed low yet
    A_len = top["price"] - a_low["price"]
    if A_len <= 0:
        return out

    b_high = post[1] if len(post) > 1 and post[1]["kind"] == "H" else None
    if b_high is None:                               # wave B forming off the A low
        b_retr = (x - a_low["price"]) / A_len
        out["abc_leg"] = "B" if x > a_low["price"] else "A"
        out["abc_type"] = "zigzag" if b_retr < ABC_ZIGZAG_MAX_B else "flat"
        out["c_tgt_hi"] = x - ABC_C_LO * A_len
        out["c_tgt_lo"] = x - ABC_C_HI * A_len
        return out

    b_retr = (b_high["price"] - a_low["price"]) / A_len
    out["abc_type"] = "zigzag" if b_retr < ABC_ZIGZAG_MAX_B else "flat"
    out["c_tgt_hi"] = b_high["price"] - ABC_C_LO * A_len
    out["c_tgt_lo"] = b_high["price"] - ABC_C_HI * A_len
    out["abc_leg"] = "C" if x < b_high["price"] else "B"
    if len(post) > 3:                                # extra legs → complex / combination
        out["abc_type"] = "combo"
    return out


def _wave_features(rs_s, params=None, price=None):
    """Full-history wave-feature columns for a ticker's raw RS line. One O(n)
    pass advances a pointer through the ZigZag so each bar sees only pivots
    confirmed by then (no lookahead). An active post-wave-5 ABC correction
    (`_abc_state`) overrides the impulse labels with wave-A/B/C. RSI divergence
    is read off the same pivots on BOTH the RS line and (when `price` is given)
    absolute price. Columns: wv_trend, wv_leg, wave_label, retrace_pct, ext_ratio,
    fib_target, fib_w1, gp_q, ext_q, abc_type, c_tgt_lo, c_tgt_hi, w4_zone,
    div_rs, div_px, cur_rs."""
    raw = _zigzag_swings(rs_s, float(_p(params, "ZIGZAG_K")))
    v = rs_s.to_numpy(dtype=float)
    n = len(v)
    rsi_rs = _rsi(rs_s).to_numpy(dtype=float)
    rsi_px = (_rsi(price.reindex(rs_s.index)).to_numpy(dtype=float)
              if price is not None else np.full(n, np.nan))
    trend = np.empty(n, dtype=object); leg = np.empty(n, dtype=object)
    wave  = np.empty(n, dtype=object); abct = np.empty(n, dtype=object)
    drs   = np.empty(n, dtype=object); dpx = np.empty(n, dtype=object)
    trend[:], leg[:], wave[:], abct[:] = "none", "none", "—", "—"
    drs[:], dpx[:] = "none", "none"
    retr = np.full(n, np.nan); ext = np.full(n, np.nan)
    ftg  = np.full(n, np.nan); fw1 = np.full(n, np.nan)
    gpq  = np.zeros(n); extq = np.zeros(n); flag = np.zeros(n)
    clo  = np.full(n, np.nan); chi = np.full(n, np.nan); w4z = np.full(n, np.nan)
    j, vis = 0, []
    for e in range(n):
        while j < len(raw) and raw[j]["confirm"] <= e:    # ZigZag pivots confirmed by bar e
            vis.append(raw[j]); j += 1
        feat = _classify_wave(vis, v[e])
        trend[e], leg[e], wave[e] = feat["trend"], feat["leg_kind"], feat["wave"]
        retr[e], ext[e] = feat["retrace"], feat["ext"]
        ftg[e], fw1[e]  = feat["fib_target"], feat["fib_w1"]
        gpq[e], extq[e] = feat["gp_q"], feat["ext_q"]
        flag[e] = _flag_signal(vis, v, e)
        abc = _abc_state(vis, v[e])
        if abc:                                       # ABC correction overrides the impulse labels
            leg[e], wave[e] = "abc", "wave-" + abc["abc_leg"]
            abct[e] = abc["abc_type"]
            clo[e], chi[e], w4z[e] = abc["c_tgt_lo"], abc["c_tgt_hi"], abc["w4_zone"]
        drs[e] = _divergence(vis, v[e], rsi_rs, e)
        dpx[e] = _divergence(vis, v[e], rsi_px, e)
    return pd.DataFrame({
        "wv_trend": trend, "wv_leg": leg, "wave_label": wave,
        "retrace_pct": retr, "ext_ratio": ext, "fib_target": ftg, "fib_w1": fw1,
        "gp_q": gpq, "ext_q": extq, "flag": flag,
        "abc_type": abct, "c_tgt_lo": clo, "c_tgt_hi": chi, "w4_zone": w4z,
        "div_rs": drs, "div_px": dpx, "cur_rs": v,
    }, index=rs_s.index)


def _combine_div(div_rs, div_px):
    """Fold the RS-line and price divergence into one direction per bar."""
    bull = (div_rs == "bull") | (div_px == "bull")
    bear = (div_rs == "bear") | (div_px == "bear")
    return np.where(bull & ~bear, "bull", np.where(bear & ~bull, "bear", "none"))


def _wave_signal_cols(f):
    """Per-bar SIGNED wave signal (vectorized) in roughly [-1, +1]: a bullish
    golden-pocket / wave-C bottom is +gp_q, a down-leg pocket bounce is −gp_q, an
    up-impulse nearing its Fib max is −ext_q. Used to blend a timeframe's
    golden-pocket reading into the cross-TF conviction."""
    trend, leg, wave = f["wv_trend"].to_numpy(), f["wv_leg"].to_numpy(), f["wave_label"].to_numpy()
    gp, ex = f["gp_q"].to_numpy(dtype=float), f["ext_q"].to_numpy(dtype=float)
    cur, chi = f["cur_rs"].to_numpy(dtype=float), f["c_tgt_hi"].to_numpy(dtype=float)
    out = np.zeros(len(f))
    up_corr = (trend == "up") & (leg == "corrective") & np.isin(wave, ["wave-2", "wave-4"])
    dn_corr = (trend == "down") & (leg == "corrective") & (wave == "wave-2")
    up_imp  = (trend == "up") & (leg == "impulse") & np.isin(wave, ["wave-3", "wave-5"])
    abc_c   = (leg == "abc") & (wave == "wave-C") & np.isfinite(cur) & np.isfinite(chi) & (cur <= chi)
    out = np.where(up_corr, gp, out)
    out = np.where(dn_corr, -gp, out)
    out = np.where(up_imp, -ex, out)
    out = np.where(abc_c, 0.8, out)
    return out


def _wave_signal_scalar(ws):
    """Scalar `_wave_signal_cols` for the active (weekly) structure read off `ws`."""
    trend, leg, wave = ws["trend"], ws["leg_kind"], ws["wave"]
    gp, ex = float(ws.get("gp_q") or 0.0), float(ws.get("ext_q") or 0.0)
    cur, chi = ws.get("cur"), ws.get("c_tgt_hi")
    if leg == "corrective" and trend == "up" and wave in ("wave-2", "wave-4"):
        return gp
    if leg == "corrective" and trend == "down" and wave == "wave-2":
        return -gp
    if leg == "impulse" and trend == "up" and wave in ("wave-3", "wave-5"):
        return -ex
    if leg == "abc" and wave == "wave-C" and np.isfinite(cur) and np.isfinite(chi) and cur <= chi:
        return 0.8
    return 0.0


def _reindex_asof(series, target_index, shift, fill):
    """As-of map an MTF series onto the active dates, forward-filled. `shift` (in
    MTF bars) is applied first so a not-yet-complete higher-TF bar can't be read
    early. tz-aware (1h) indexes are normalised to naïve dates first."""
    try:
        if series is None or series.empty:
            return None
        s = series.shift(shift) if shift else series
        idx = s.index
        if getattr(idx, "tz", None) is not None:
            s = s.copy(); s.index = idx.tz_localize(None)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        return s.reindex(target_index, method="ffill").fillna(fill)
    except Exception:
        return None
