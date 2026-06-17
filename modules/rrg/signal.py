"""
RRG signal math — the decoupled core.

Two coordinate systems, sharing the quadrant boundary at 100:

  * SIGNAL space — interval-independent, σ-normalized about the *true* boundary
    (RS-Ratio = 100 where fast EMA == slow EMA; RS-Momentum = 0). Everything
    functional reads these: quadrant, Elliott-wave phase, the rotation gates and
    the call. Because both daily and weekly are scaled by their own rolling σ,
    one set of gate constants means the same thing on both intervals.

  * DISPLAY space — cosmetic only. A scalar gain on the signal coords with the
    offset pinned to *exactly* 100, so the chart's drawn cross and the frontend's
    quadrant/crossing logic stay in lockstep with the calls. Display is a pure
    multiple of signal, so the visual tail shape == the shape the calls read.

This replaces the old design, where a daily affine map (fitted to a reference
image that no longer exists) and a weekly z-stretch "to fill the frame" were
entangled with the calls — a cosmetic x-offset of 100.55 even shifted the
quadrant boundary off the true RS=100 line, and the gates were denominated in
chart units that differed between intervals.
"""

import logging
import math
import time

import yfinance as yf
import pandas as pd
import numpy as np

from modules.confluence import flags, exhaustion, volume_profile, accumulation
from modules.confluence import wave as wave_eng

# yfinance logs every transient bulk-download hiccup (e.g. the intermittent
# "1 Failed download: ['XLB']: TypeError('NoneType' object is not subscriptable)")
# at ERROR level. We own retry + fail-soft for those (see `_fetch_close`), so quiet
# the library's per-download error spam to keep the server console readable.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ---------------------------------------------------------------------------
# Public config (re-exported by __init__ — schwab imports these)
# ---------------------------------------------------------------------------

DEFAULT_TICKERS = [
    "XLK",   # Technology
    "XLE",   # Energy
    "XLV",   # Health Care
    "XLF",   # Financials
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLI",   # Industrials
    "XLB",   # Materials
    "XLU",   # Utilities
    "XLRE",  # Real Estate
    "XLC",   # Communication Services
]

SECTOR_NAMES = {
    "XLK": "Technology",       "XLE": "Energy",          "XLV": "Health Care",
    "XLF": "Financials",       "XLY": "Cons. Discretionary", "XLP": "Cons. Staples",
    "XLI": "Industrials",      "XLB": "Materials",       "XLU": "Utilities",
    "XLRE": "Real Estate",     "XLC": "Communication",
}

BENCHMARK = "SPY"

# ---------------------------------------------------------------------------
# Searchable parameters (the only knobs the walk-forward backtest tunes).
# Kept in one dict and threaded as an argument — never mutated globally — so a
# backtest trial can't corrupt a concurrent live chart request.
# ---------------------------------------------------------------------------

# Searchable params for the wave engine. ZIGZAG_K shapes the significant-swing
# detection (it changes compute_series output, so the backtest caches series by
# it); T_* are the conviction thresholds that turn the confluence score into a
# call. TREND_ETA / MOM_TAU drive only the DISPLAY momentum squash. Re-bake
# ZIGZAG_K / T_* from backtest.walk_forward_search against forward returns.
DEFAULTS = {
    "TREND_ETA":   1.00,   # tanh scale for the DISPLAY soft trend direction
    "MOM_TAU":     0.11,   # DISPLAY EW corrective cap, in raw-momentum units
    **wave_eng._WAVE_DEFAULTS,   # ZIGZAG_K — the wave engine owns this searchable default
    "T_IN":        40.0,   # conviction ≥ this → ROTATE IN (a bottom/entry setup)
    "T_WATCH":     20.0,   # T_WATCH ≤ conviction < T_IN → WATCH (arming)
    "T_WARN":      40.0,   # bearish conviction past this → exit (⚠️ extended / ROTATE OUT)
}

RS_SMOOTH   = 2     # light EMA span on the raw RS line before swing detection (de-noise)


# Conviction model. Each factor contributes weight toward a signed score
# (bullish +, bearish −); a confluence of several = high conviction. Timeframe
# weights scale by degree (monthly > weekly > daily > 1h). The score is clamped to
# the display range and is a RELATIVE confluence reading, not a calibrated odds.
TF_WEIGHT   = {"1mo": 1.00, "1wk": 0.80, "1d": 0.50, "1h": 0.25}
W_GP        = 60.0   # max golden-pocket contribution (× depth-quality × TF weight)
W_DIV       = 18.0   # each RSI divergence (RS or price) × TF weight
W_W4ZONE    = 15.0   # C-bottom sitting in the prior wave-4 confluence zone
W_CLEAN     = 10.0   # clean (unambiguous) wave count
W_VOL_EXH   = 14.0   # volume buyer/seller exhaustion (× TF weight) — topping/bottoming tell
W_VOL_PROFILE = 12.0 # volume-profile structure (× signed strength × TF weight) — price in
                     # discount/premium vs its value area; a value-buy/value-sell confluence
W_ACCUM     = 13.0   # accumulation/distribution (× signed strength × TF weight) — U/D volume
                     # ratio + A/D-line divergence = the institutional buy/sell footprint.
                     # LIVE-only (real sectors), trailing/no-lookahead → promotable to backtest.

# Flag contribution is now EMPIRICAL + REGIME-AWARE (replaced the flat W_FLAG=25):
# the weight is the flag's measured edge `(win_rate − 0.5)`, scaled by W_FLAG_EDGE,
# using the symbol's own win rate when we have enough events (`flag_win_*`/`flag_n_*`
# in `ws`) else the basket default, and zeroed when the flag opposes the regime
# (bear flag in a HEALTHY market fails upward). win rates: signal.flag_win_rates_for
# (live, in-memory cached); basket defaults baked from flag_backtest --regime.
W_FLAG_EDGE   = 150.0
# Basket defaults (used when a symbol has < FLAG_MIN_N of its own flag events).
# From `flag_backtest --regime` (5y daily, +10-bar continuation, breadth regime over
# the available 3y):
#   bull (outside DETERIORATING): 62.3% (n=69); unconditioned ETF basket 60.6% (n=99);
#     broad 96-symbol universe 55.5% → 0.58 is a deliberate midpoint prior (conviction
#     runs on both ETFs and theme baskets).
#   bear (inside DETERIORATING): 0% on only n=5 — the 3y window had no real bear market,
#     just brief bull-market pullbacks, so bear flags still failed UPWARD even when
#     conditioned (unconditioned ETF bear is 27.4%, n=73). There is therefore NO measured
#     bear-flag edge → 0.50 = "no edge assumed" (edge clamps to 0). The theory that bear
#     flags work in a genuine downtrend stays untested until a bear market supplies data;
#     a per-symbol stock with bear_n ≥ FLAG_MIN_N and a real edge still contributes.
FLAG_BASE_WIN = {"bull": 0.58, "bear": 0.50}
FLAG_MIN_N    = 8       # fewer than this many events → distrust per-symbol, use basket default
FLAG_OPPOSING = 0.0     # weight multiplier for a flag opposing the current regime

# ROTATION-REGIME GATE. The backtest shows ROTATE IN only earns positive forward
# excess when rotation is LIVE (equal-weight RSP leading cap-weight SPY); in a
# concentration regime ("off") entries lose (≈−0.9% excess at +10/20d). So we
# SUPPRESS bullish conviction when rotation is off — gating out the low-conviction
# off-regime entries lifted equity (+67→+81%) and halved max drawdown (−21→−14%).
# We do NOT boost when on: a positive on-tilt only drags marginal entries over the
# T_IN line and dilutes the good bucket (let on-entries fire on their own merit).
# The regime is RSP/SPY vs its EMA — trailing, no-lookahead — so this applies in the
# BACKTEST too (unlike the per-symbol flag win rate, which is live-only). Judgment-
# fixed, not searched.
W_ROTATION_OFF = 30.0    # subtracted from conviction in a concentration regime
W_ROTATION_ON  = 0.0     # no boost when broadening (boosting dilutes the good bucket)
ROTATION_MA    = 50      # EMA span (daily bars) for the RSP/SPY trend

CONV_HI, CONV_LO = 99.0, -70.0          # display clamp (bullish max / bearish max)

# The two extension warnings are first-class rotation calls (not a side flag), so
# the event study scores them on their own forward returns. Downstream maps them
# to a TRIM action / amber badge. Single source of truth for every consumer.
WARN_CALLS = ("⚠️ w3 extended", "⚠️ w5 extended")


def _p(params, key):
    return (params or {}).get(key, DEFAULTS[key])


def _jsafe(x, places=3):
    """Round to JSON-safe float, or None for NaN/inf/non-numeric."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else round(f, places)


# ---------------------------------------------------------------------------
# Interval lenses (the time-scale of the view — legitimately interval-specific,
# NOT cosmetic). Unification is about the normalization + gates, not these spans.
# ---------------------------------------------------------------------------

TREND_D = 60     # trading days for the soft trend direction (daily)
TREND_W = 12     # bars for the soft trend direction (weekly)

LENS = {
    # interval: (fast_ema, slow_ema, mom_diff, mom_smooth, trend_len, ratio_win, mom_win)
    "1wk": (10,  40,  5, 3, TREND_W, 52,  26),
    "1d":  (50, 140, 15, 3, TREND_D, 140, 60),
}
PERIOD = "3y"

# Display gains (cosmetic only — chosen to fill the ~90–116 / 94–106 frame for a
# typical ±3σ cloud). Changing these moves dots on the chart and nothing else.
DISP_GAIN_X = 4.0
DISP_GAIN_Y = 2.2

# Non-searched signal constants, all now in σ-units.
DIR_EPS      = 0.05   # per-step σ move to register a direction arrow
FLAT_MAG     = 0.30   # net σ displacement below which a tail reads "flat"
STRENGTH_DIV = 3.0    # σ of cumulative path mapped to strength 100


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _jdk_ratio_signal(series, fast_span, slow_span):
    """JdK-style RS-Ratio: fast_EMA / slow_EMA * 100. Stays above 100 while the
    sector leads (fast > slow)."""
    fast = series.ewm(span=fast_span, adjust=False).mean()
    slow = series.ewm(span=slow_span, adjust=False).mean()
    return fast / slow * 100


def _scale(series, window, center):
    """σ-normalize about a FIXED center (the true boundary), not the rolling
    mean. Returns (series − center) / rolling_std — centered at 0 at the
    boundary, in σ-units so gates are interval-independent. (A rolling-mean
    z-score would recenter on drifting history and break boundary honesty —
    that was the old weekly bug, where "Leading" meant "above its own average"
    rather than "fast EMA above slow EMA".)"""
    minp = max(20, window // 2)
    sd   = series.rolling(window, min_periods=minp).std(ddof=0)
    return (series - center) / sd.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Tail geometry / scoring (fed SIGNAL coords; all thresholds now in σ-units)
# ---------------------------------------------------------------------------

def _quadrant(ratio, momentum):
    if ratio >= 100:
        return "Leading" if momentum >= 100 else "Weakening"
    return "Improving" if momentum >= 100 else "Lagging"


def _direction(ratios, moments):
    if len(ratios) < 2:
        return "·"
    dr = ratios[-1] - ratios[-2]
    dm = moments[-1] - moments[-2]
    h  = "→" if dr > DIR_EPS else ("←" if dr < -DIR_EPS else "·")
    v  = "↑" if dm > DIR_EPS else ("↓" if dm < -DIR_EPS else "·")
    return h + v


_COMPASS = [
    (1, 0, "→"), (1, 1, "↗"), (0, 1, "↑"), (-1, 1, "↖"),
    (-1, 0, "←"), (-1, -1, "↙"), (0, -1, "↓"), (1, -1, "↘"),
]


def _tail_heading(ratios, moments):
    """Overall tail direction as an 8-point compass arrow, fit to the net
    displacement from first to last point. Returns (arrow, label, angle°)."""
    if len(ratios) < 2:
        return "·", "flat", None
    dr  = ratios[-1] - ratios[0]
    dm  = moments[-1] - moments[0]
    mag = math.hypot(dr, dm)
    if mag < FLAT_MAG:
        return "·", "flat", None
    angle = math.degrees(math.atan2(dm, dr)) % 360
    idx   = int((angle + 22.5) // 45) % 8
    arrow = _COMPASS[idx][2]
    label = {
        "→": "E", "↗": "NE", "↑": "N", "↖": "NW",
        "←": "W", "↙": "SW", "↓": "S", "↘": "SE",
    }[arrow]
    return arrow, label, round(angle, 1)


def _tail_strength(ratios, moments):
    """Cumulative σ path length mapped to 0–100 (stubby tail = low score)."""
    if len(ratios) < 2:
        return 0
    path = sum(
        math.hypot(ratios[i] - ratios[i - 1], moments[i] - moments[i - 1])
        for i in range(1, len(ratios))
    )
    return int(max(0, min(100, round(path / STRENGTH_DIV * 100))))


def _accum(ratios, moments):
    """Accumulation/rotate-IN ranking score (0–100). Embeds 'momentum turns
    first'. Coefficients are σ-tuned for spread — a ranking heuristic, not a
    call gate (the call is decided in _rotation_call)."""
    if len(ratios) < 2:
        return 0
    mom_slope = moments[-1] - moments[0]
    kick      = max(0.0, moments[-1] - moments[-2])
    room      = min(max(0.0, 100 - ratios[-1]), 3.0)
    return int(max(0, min(100, round(50 + 16 * mom_slope + 8 * kick + 5 * room))))


def _distrib(ratios, moments):
    """Distribution/rotate-OUT ranking score (0–100). Mirror of _accum."""
    if len(ratios) < 2:
        return 0
    mom_slope = moments[0] - moments[-1]
    kick      = max(0.0, moments[-2] - moments[-1])
    extended  = min(max(0.0, ratios[-1] - 100), 3.0)
    return int(max(0, min(100, round(50 + 16 * mom_slope + 8 * kick + 5 * extended))))


def _confluence_note(ws, direction):
    """' · confluence: …' tag for the call reason — which corroborating signals
    agree with `direction` ('bull'/'bear'): dual RSI (RS line + price), and the
    higher / lower timeframe divergence."""
    tags = []
    if ws.get("div_rs") == direction and ws.get("div_px") == direction:
        tags.append("RS+price")
    if ws.get("htf_div") == direction:
        tags.append("HTF")
    if ws.get("ltf_div") == direction:
        tags.append("LTF")
    return (" · confluence: " + "+".join(tags)) if tags else ""


def _div_confluence(ws):
    """Compact divergence summary for the tooltip ('bullish: RS+price+HTF') or
    None — lists every timeframe/series diverging the same way."""
    for d, label in (("bull", "bullish"), ("bear", "bearish")):
        srcs = [name for name, key in (("RS", "div_rs"), ("price", "div_px"),
                                       ("HTF", "htf_div"), ("LTF", "ltf_div"))
                if ws.get(key) == d]
        if srcs:
            return f"{label}: {'+'.join(srcs)}"
    return None


def _mtf_features(symbols, benchmark, interval, params, period=PERIOD):
    """{ticker: DataFrame[contrib(float), div(str)]} on `interval`. `contrib` = the
    signed wave signal + a fraction of the combined RSI divergence — one number to
    blend that timeframe into conviction. Best-effort: any fetch/compute failure
    (e.g. 1h history unavailable) → {}."""
    try:
        close = _fetch_close(list(symbols) + [benchmark], interval, period)
    except Exception:
        return {}
    if close is None or close.empty or benchmark not in close.columns:
        return {}
    bench = close[benchmark]
    out = {}
    for t in symbols:
        if t not in close.columns or close[t].dropna().empty:
            continue
        rs   = (close[t] / bench) * 100
        rs_s = rs.ewm(span=RS_SMOOTH, adjust=False).mean()
        f    = wave_eng._wave_features(rs_s, params, price=close[t])
        div  = wave_eng._combine_div(f["div_rs"].to_numpy(), f["div_px"].to_numpy())
        dsig = np.where(div == "bull", 1.0, np.where(div == "bear", -1.0, 0.0))
        out[t] = pd.DataFrame({"contrib": wave_eng._wave_signal_cols(f) + 0.4 * dsig, "div": div},
                              index=f.index)
    return out


# Timeframes blended into conviction beyond the active (weekly) structure, with
# the no-lookahead shift and which one feeds the divergence-note columns.
_MTF_SPECS = [("mtf_1mo", "1mo", PERIOD, 1, "htf_div"),
              ("mtf_1d",  "1d",  PERIOD, 0, None),
              ("mtf_1h",  "1h",  "730d", 0, "ltf_div")]


def _attach_mtf(out, symbols, benchmark, interval, params):
    """Add per-TF blended contributions (mtf_1mo / mtf_1d / mtf_1h) and the
    htf_div / ltf_div note strings to each ticker frame (best-effort)."""
    data = {tag: _mtf_features(symbols, benchmark, iv, params, per)
            for tag, iv, per, _sh, _dv in _MTF_SPECS}
    for t, df in out.items():
        for tag, iv, per, shift, divcol in _MTF_SPECS:
            d = data[tag].get(t)
            c = wave_eng._reindex_asof(d["contrib"], df.index, shift, 0.0) if d is not None else None
            df[tag] = c.values if c is not None else 0.0
            if divcol:
                v = wave_eng._reindex_asof(d["div"], df.index, shift, "none") if d is not None else None
                df[divcol] = v.values if v is not None else "none"


def _flag_regime_factor(kind, regime):
    """Weight multiplier for a flag given the current market regime. A flag that
    OPPOSES the regime is down-weighted toward 0 (bear flags fail upward in a
    HEALTHY market; bull flags fail in a DETERIORATING one). Aligned or unknown
    regime → full weight."""
    if regime == "HEALTHY":
        return FLAG_OPPOSING if kind == "bear" else 1.0
    if regime == "DETERIORATING":
        return FLAG_OPPOSING if kind == "bull" else 1.0
    return 1.0   # NEUTRAL / None / unknown


def _conviction(ws, params=None):
    """Signed confluence score (bullish + / bearish −), clamped to the display
    range, plus a factor breakdown for the tooltip. Each factor CONTRIBUTES weight
    — a confluence of several = high conviction; none is individually mandatory
    (the probabilistic replacement for the old AND-gates). Reads the single
    (weekly) structure + the HTF/LTF divergence here; Phase 2 will sum the
    golden-pocket factor across 1h/daily/weekly/monthly with TF weights."""
    trend, wave = ws["trend"], ws["wave"]
    cur, c_hi, w4 = ws["cur"], ws.get("c_tgt_hi"), ws.get("w4_zone")
    bull = bear = 0.0
    factors = []

    def add(amt, label):                       # signed amt (+bull / −bear)
        nonlocal bull, bear
        if abs(amt) <= 1e-9:
            return
        if amt > 0:
            bull += amt
        else:
            bear += -amt
        factors.append([label, round(amt, 1)])

    # Golden-pocket / wave structure, blended across timeframes (signed). The
    # active WEEKLY structure plus the monthly / daily / 1h reads — daily+weekly
    # alignment (the common case) stacks; a lone timeframe contributes its degree.
    add(W_GP * wave_eng._wave_signal_scalar(ws) * TF_WEIGHT["1wk"], "wk wave")
    for tag, tf in (("mtf_1mo", "1mo"), ("mtf_1d", "1d"), ("mtf_1h", "1h")):
        c = float(ws.get(tag) or 0.0)
        add(W_GP * c * TF_WEIGHT[tf], tf + " wave")
    # Active-timeframe RSI divergence (RS line + price); the other TFs' divergence
    # is already folded into their mtf contribution above.
    for key, lab in (("div_rs", "RS"), ("div_px", "px")):
        d = ws.get(key)
        if d in ("bull", "bear"):
            add((W_DIV if d == "bull" else -W_DIV) * TF_WEIGHT["1wk"], lab + (" div↑" if d == "bull" else " div↓"))
    # bull/bear flag continuation pattern (active timeframe) — weighted by the
    # flag's EMPIRICAL edge (per-symbol win rate where available, else basket
    # default) and the REGIME (a flag opposing the regime is zeroed). f = ±1.
    f = float(ws.get("flag") or 0.0)
    if abs(f) > 1e-9:
        kind = "bull" if f > 0 else "bear"
        win  = ws.get("flag_win_" + kind)
        if win is None or float(ws.get("flag_n_" + kind, 0) or 0) < FLAG_MIN_N:
            win = FLAG_BASE_WIN[kind]
        edge = max(0.0, float(win) - 0.5)              # never flip the sign; toward 0
        rf   = _flag_regime_factor(kind, ws.get("regime"))
        add(W_FLAG_EDGE * edge * rf * f * TF_WEIGHT["1wk"], kind + " flag")
    # volume buyer/seller exhaustion (the symbol's own price+volume): a selling
    # climax is bullish (bottoming), a buying climax bearish (topping). Confluence
    # for the wave engine, which can't see volume on the RS line.
    ex = ws.get("vol_exh")
    if ex == "seller":
        add(W_VOL_EXH * TF_WEIGHT["1wk"], "sell exhaustion")
    elif ex == "buyer":
        add(-W_VOL_EXH * TF_WEIGHT["1wk"], "buy exhaustion")
    # volume-profile structure (the symbol's absolute price vs its value area): price
    # in DISCOUNT (below value) is a value-buy → bullish, in PREMIUM bearish; a
    # bottoming (b) / topping (P) profile shape reinforces. Graded strength [-1,1]
    # from the confluence leaf — confluence the RS-line wave engine can't see.
    vp = ws.get("vol_profile")
    if vp:
        vp_s, vp_lab = volume_profile.contribution(vp)
        add(W_VOL_PROFILE * vp_s * TF_WEIGHT["1wk"], vp_lab or "vol profile")
    # accumulation/distribution (the symbol's own price+volume): net buying (U/D > 1,
    # rising A/D line, more accumulation days) is bullish, net selling bearish; an
    # A/D-vs-price divergence amplifies. The "big money" demand tell the wave engine
    # (RS line, no volume) and the location-only volume profile can't see.
    ac = ws.get("accumulation")
    if ac:
        ac_s, ac_lab = accumulation.contribution(ac)
        add(W_ACCUM * ac_s * TF_WEIGHT["1wk"], ac_lab or "accumulation")
    # prior wave-4 confluence on a wave-C bottom
    if wave == "wave-C" and np.isfinite(cur) and np.isfinite(c_hi) and cur <= c_hi \
            and np.isfinite(w4) and abs(w4) > 1e-9 and abs(cur - w4) <= 0.10 * abs(w4):
        add(W_W4ZONE, "prior-w4 zone")
    # clean-count bonus toward the leaning side (only when there's a lean — a
    # tie, e.g. a wave-A with no factors, must not get a phantom bullish +10)
    if trend in ("up", "down") and wave != "—" and abs(bull - bear) > 1e-9:
        add(W_CLEAN if bull > bear else -W_CLEAN, "clean count")

    # Market-wide rotation gate, applied LAST so the clean-count still reflects the
    # wave/confluence lean, not the regime. Off (concentration) suppresses entries;
    # on (broadening) mildly supports them. Unknown → no tilt (fail-soft).
    rot = ws.get("rotation")
    if rot == "off":
        add(-W_ROTATION_OFF, "rotation off")
    elif rot == "on":
        add(W_ROTATION_ON, "rotation on")

    return round(max(CONV_LO, min(CONV_HI, bull - bear)), 1), factors


def _rotation_call(ws, params=None):
    """Wave/Fib state → (call, why). The discrete call is derived from the signed
    confluence conviction (`_conviction`) crossing thresholds, with the wave
    context selecting which call. `call` stays in the 7-value CALL_ORDER set, so
    the backtester / schwab / screener / themes are unchanged."""
    score = ws.get("_score")
    if score is None:
        score, _ = _conviction(ws, params)
    trend, leg, wave = ws["trend"], ws["leg_kind"], ws["wave"]
    t_in, t_watch, t_warn = _p(params, "T_IN"), _p(params, "T_WATCH"), _p(params, "T_WARN")
    conf, conf_bear = _confluence_note(ws, "bull"), _confluence_note(ws, "bear")

    # Post-wave-5 ABC correction takes precedence over the impulse labels.
    if leg == "abc":
        if wave == "wave-A":
            return "ROTATE OUT", "Wave A off a wave-5 top — the correction is underway"
        if wave == "wave-B":
            if ws.get("abc_type") == "zigzag":
                return "ROTATE OUT", "Wave-B bounce in a zigzag — sell the bounce, wave C lower ahead"
            return "WATCH", "Wave-B of a flat correction — choppy, no edge"
        if wave == "wave-C":
            if score >= t_in:
                return "ROTATE IN", "Wave-C bottom — confluence confirms the turn" + conf
            if score >= t_watch:
                return "WATCH", "Wave-C nearing its target / prior-w4 zone — conviction building" + conf
            return "AVOID", "Wave-C still falling toward its target — not done correcting"
        return "WATCH", "Corrective structure — standing aside"

    if trend in ("none", "ambiguous") or wave == "—":
        return "WATCH", "No clean wave/Fib setup — standing by"

    if trend == "up":
        if leg == "corrective":
            if score >= t_in:
                return "ROTATE IN", wave + " retrace with confluence — entry" + conf
            if score >= t_watch:
                return "WATCH", wave + " retrace forming — conviction building" + conf
            return "WATCH", "Pullback without enough confluence yet"
        if leg == "impulse":
            if score <= -t_warn:
                warn = "⚠️ w5 extended" if wave == "wave-5" else "⚠️ w3 extended"
                return warn, wave + " extended with exhaustion confluence — trim into strength" + conf_bear
            return "HOLD", wave + " impulse underway — hold the leader"
        return "WATCH", "No actionable wave state"

    # trend == "down" — mirror (a sector losing relative strength)
    if leg == "corrective":
        if score <= -t_warn:
            return "ROTATE OUT", "Counter-trend bounce into the down-leg pocket — sell into it" + conf_bear
        if wave == "wave-4":
            return "AVOID", "Shallow bounce in a down-trend — continuation lower likely"
        return "AVOID", "Weak bounce in a down-trend — nothing to buy"
    if leg == "impulse":
        return "AVOID", "Down-impulse not finished — stay out"
    return "WATCH", "No actionable wave state"


# ---------------------------------------------------------------------------
# Price data (one cached yfinance path, shared by the chart and the backtest)
# ---------------------------------------------------------------------------

_PRICE_CACHE = {}     # (interval, sorted symbols) -> (fetched_at, close_df)
_PRICE_TTL   = 600    # seconds


def _download_close(symbols, interval, period):
    """One yfinance close pull → DataFrame (one column per symbol). Handles both
    the multi-symbol (MultiIndex) and single-symbol (flat) column shapes."""
    raw = yf.download(
        symbols, period=period, interval=interval,
        auto_adjust=True, progress=False, group_by="column",
    )
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            return raw["Close"].copy()
        return pd.DataFrame()
    if "Close" in raw.columns:                       # single symbol → flat columns
        return raw["Close"].to_frame(symbols[0])
    return pd.DataFrame()


def _fetch_close(symbols, interval, period=PERIOD):
    key = (interval, tuple(sorted(symbols)))
    hit = _PRICE_CACHE.get(key)
    if hit and time.time() - hit[0] < _PRICE_TTL:
        return hit[1]
    close = _download_close(list(symbols), interval, period)
    # yfinance intermittently drops a symbol or two from a bulk pull (a transient
    # rate-limit / 'NoneType' hiccup). Re-fetch just the missing ones a couple of
    # times so one bad draw doesn't silently strip a theme constituent or sector.
    for _ in range(2):
        missing = [s for s in symbols
                   if s not in close.columns or close[s].dropna().empty]
        if not missing:
            break
        time.sleep(0.8)
        retry = _download_close(missing, interval, period)
        for s in missing:
            if s in retry.columns and not retry[s].dropna().empty:
                close[s] = retry[s]
    close = close.dropna(how="all").ffill()
    _PRICE_CACHE[key] = (time.time(), close)
    return close


_OHLC_CACHE = {}      # (sorted symbols, period) -> (fetched_at, dict_of_field_frames)


def fetch_ohlc(symbols, period=PERIOD):
    """Daily OHLC+V for the backtest (next-bar-open entry + ATR exits) and the
    flag win-rate / exhaustion reads (volume). yfinance auto-adjusts the price
    fields consistently."""
    key = (tuple(sorted(symbols)), period)
    hit = _OHLC_CACHE.get(key)
    if hit and time.time() - hit[0] < _PRICE_TTL:
        return hit[1]
    raw = yf.download(symbols, period=period, interval="1d",
                      auto_adjust=True, progress=False, group_by="column")
    out = {}
    for field in ("Open", "High", "Low", "Close", "Volume"):
        if isinstance(raw.columns, pd.MultiIndex):
            if field in raw.columns.get_level_values(0):
                out[field.lower()] = raw[field]
        elif field in raw.columns:
            out[field.lower()] = raw[field].to_frame()
    _OHLC_CACHE[key] = (time.time(), out)
    return out


# ---------------------------------------------------------------------------
# Empirical flag weighting + regime gate (live conviction refinement). These
# reach UP to the breadth module for the market regime — lazily and fail-soft,
# since `breadth → schwab → rrg` means rrg must not import breadth at module load.
# ---------------------------------------------------------------------------

_REGIME_CACHE = {"at": 0.0, "value": None}
_REGIME_TTL   = 600     # seconds
_FLAG_WR_CACHE = {}     # symbol -> {"bull", "bull_n", "bear", "bear_n"} (process lifetime)
FLAG_WR_PERIOD = "5y"   # history for a stable per-symbol flag win-rate (more events)


def current_regime():
    """Current market regime label (HEALTHY / NEUTRAL / DETERIORATING) from the
    breadth module, in-memory cached ~10 min. Fail-soft → None (the conviction
    flag gate then treats the regime as unknown = full weight)."""
    now = time.time()
    if now - _REGIME_CACHE["at"] < _REGIME_TTL:
        return _REGIME_CACHE["value"]
    value = None
    try:
        from modules.breadth import _full_series
        from modules.breadth import regime as breadth_regime
        agg, der, _index = _full_series("sp500")
        if agg is not None:
            labels = breadth_regime.regime_series(der["summation"], agg["pct_above_200"])
            if len(labels):
                value = str(labels.iloc[-1])
    except Exception:
        value = None
    _REGIME_CACHE.update(at=now, value=value)
    return value


_ROTATION_CACHE = {"at": 0.0, "value": None}


def _rotation_label(rsp, spy, span=ROTATION_MA):
    """Per-date 'on'/'off' Series from the RSP/SPY ratio vs its EMA. 'on' = equal-
    weight leading cap-weight = breadth broadening (rotation live). Trailing EMA →
    no-lookahead. Empty Series if the ratio can't be formed."""
    ratio = (rsp / spy).dropna()
    if ratio.empty:
        return pd.Series(dtype=object)
    ma = ratio.ewm(span=span, adjust=False).mean()
    return pd.Series(np.where(ratio > ma, "on", "off"), index=ratio.index)


def rotation_regime():
    """Current rotation regime ('on'/'off') from RSP vs SPY, in-memory cached
    ~10 min. Fail-soft → None (the conviction gate then applies no tilt)."""
    now = time.time()
    if now - _ROTATION_CACHE["at"] < _REGIME_TTL:
        return _ROTATION_CACHE["value"]
    value = None
    try:
        close = _fetch_close(["RSP", BENCHMARK], "1d", PERIOD)
        if "RSP" in close.columns and BENCHMARK in close.columns:
            lab = _rotation_label(close["RSP"], close[BENCHMARK])
            if len(lab):
                value = str(lab.iloc[-1])
    except Exception:
        value = None
    _ROTATION_CACHE.update(at=now, value=value)
    return value


def _regime_label_series(dates):
    """Per-date regime labels aligned to `dates` (for win-rate conditioning).
    Fail-soft → None."""
    try:
        from modules.breadth import _full_series
        from modules.breadth import regime as breadth_regime
        agg, der, _index = _full_series("sp500")
        if agg is None:
            return None
        labels = breadth_regime.regime_series(der["summation"], agg["pct_above_200"])
        return breadth_regime.align_labels(labels, dates)   # handles str/Timestamp mismatch
    except Exception:
        return None


def flag_win_rates_for(symbols, period=FLAG_WR_PERIOD):
    """Per-symbol regime-conditioned flag win rates from each symbol's own
    price+volume, via the shared `flags.win_rates`. Process-lifetime cached per
    symbol (win rates barely move; the user wants these computed rarely). Returns
    {symbol: {"bull", "bull_n", "bear", "bear_n"}}. Fail-soft → {} on any error.

    Used for the ~11 sector ETFs in the live conviction engine (they're not in the
    screener's stock universe, so the background `flag_winrate` table can't serve
    them). The stock universe is served by the screener precompute instead."""
    symbols = [s for s in symbols if s]
    todo = [s for s in symbols if s not in _FLAG_WR_CACHE]
    if todo:
        try:
            ohlc = fetch_ohlc(todo, period=period)
            close, vol = ohlc.get("close"), ohlc.get("volume")
            reg = _regime_label_series(close.index) if close is not None else None
            reg_arr = reg.to_numpy() if reg is not None else None
            for s in todo:
                if close is None or s not in close.columns:
                    _FLAG_WR_CACHE[s] = {"bull": None, "bull_n": 0, "bear": None, "bear_n": 0}
                    continue
                v = vol[s].to_numpy(dtype=float) if (vol is not None and s in vol.columns) else None
                _FLAG_WR_CACHE[s] = flags.win_rates(
                    close[s].to_numpy(dtype=float), v, regime_labels=reg_arr)
        except Exception:
            for s in todo:
                _FLAG_WR_CACHE.setdefault(s, {"bull": None, "bull_n": 0, "bear": None, "bear_n": 0})
    return {s: _FLAG_WR_CACHE.get(s, {}) for s in symbols}


def exhaustion_for(symbols, period=FLAG_WR_PERIOD):
    """Current volume buyer/seller exhaustion per symbol from daily OHLCV (one
    cached `fetch_ohlc`). Returns {symbol: "buyer"/"seller"/None}. Fail-soft → {}."""
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    try:
        ohlc = fetch_ohlc(symbols, period=period)
    except Exception:
        return {}
    high, low, close, vol = (ohlc.get("high"), ohlc.get("low"),
                             ohlc.get("close"), ohlc.get("volume"))
    if close is None or vol is None:
        return {}
    out = {}
    for s in symbols:
        if s in close.columns and s in vol.columns and s in high.columns and s in low.columns:
            try:                                   # series share one index → keep aligned
                out[s] = exhaustion.current_exhaustion(high[s], low[s], close[s], vol[s])
            except Exception:
                out[s] = None
    return out


def volume_profile_for(symbols, period=FLAG_WR_PERIOD):
    """Current volume-profile read per symbol from daily OHLCV (one cached
    `fetch_ohlc`, shared with the exhaustion/flag reads → no extra download).
    Returns {symbol: read dict | None}. Fail-soft → {}. A LIVE-only conviction
    refinement (real sectors only — synthetic theme indices carry no volume); the
    profile is trailing/no-lookahead, so it could later be promoted into the backtest."""
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    try:
        ohlc = fetch_ohlc(symbols, period=period)
    except Exception:
        return {}
    high, low, close, vol = (ohlc.get("high"), ohlc.get("low"),
                             ohlc.get("close"), ohlc.get("volume"))
    if close is None or vol is None:
        return {}
    out = {}
    for s in symbols:
        if s in close.columns and s in vol.columns and s in high.columns and s in low.columns:
            try:
                out[s] = volume_profile.current(high[s], low[s], close[s], vol[s])
            except Exception:
                out[s] = None
    return out


def accumulation_for(symbols, period=FLAG_WR_PERIOD):
    """Current accumulation/distribution read per symbol from daily OHLCV (one cached
    `fetch_ohlc`, shared with the exhaustion/profile reads → no extra download).
    Returns {symbol: read dict | None}. Fail-soft → {}. A LIVE-only conviction
    refinement (real sectors only — synthetic theme indices carry no volume); the
    U/D ratio + A/D line are trailing/no-lookahead, so it could later be promoted into
    the backtest (same status as volume_profile)."""
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    try:
        ohlc = fetch_ohlc(symbols, period=period)
    except Exception:
        return {}
    high, low, close, vol = (ohlc.get("high"), ohlc.get("low"),
                             ohlc.get("close"), ohlc.get("volume"))
    if close is None or vol is None:
        return {}
    out = {}
    for s in symbols:
        if s in close.columns and s in vol.columns and s in high.columns and s in low.columns:
            try:
                out[s] = accumulation.current(high[s], low[s], close[s], vol[s])
            except Exception:
                out[s] = None
    return out


# ---------------------------------------------------------------------------
# Series + per-tail evaluation
# ---------------------------------------------------------------------------

def compute_series(tickers, benchmark, interval, asof=None, params=None, close=None):
    """Per-ticker point-cadence DataFrames with both coordinate systems.

    Returns (series, date, close) where:
      series = {ticker: DataFrame[sig_ratio, sig_mom, disp_ratio, disp_mom,
                                  t_soft, mom_raw, + wave-feature columns]} at
               one row per tail point (daily → one per ISO week, last bar),
      date   = the latest benchmark bar date (str) or None,
      close  = the raw close DataFrame (for change_pct / rel_pct in the chart).

    `close` lets a caller inject a pre-built panel (one column per `ticker` +
    `benchmark`) instead of fetching — used by the themes module to run the RRG
    on synthetic equal-weight theme indices. When None, prices are fetched.
    """
    fast, slow, mom_diff, mom_smooth, trend_len, ratio_win, mom_win = LENS[interval]
    eta = _p(params, "TREND_ETA")
    tau = _p(params, "MOM_TAU")

    fetched = close is None       # injected panels (themes) skip the MTF fetch
    if close is None:
        close = _fetch_close(list(tickers) + [benchmark], interval, PERIOD)
    if asof:
        close = close.loc[:asof]
        if close.empty:
            raise ValueError(f"No price data on or before {asof}")
    bench = close[benchmark]

    out = {}
    for t in tickers:
        if t not in close.columns or close[t].dropna().empty:
            continue

        rs        = (close[t] / bench) * 100
        ratio_raw = _jdk_ratio_signal(rs, fast, slow)
        mom_raw   = ratio_raw.diff(mom_diff).ewm(span=mom_smooth, adjust=False).mean()
        t_soft    = np.tanh(ratio_raw.diff(trend_len) / eta)

        # Elliott-wave squash (display only): momentum that opposes the prevailing
        # trend is corrective and gets capped at the boundary. The rotation call
        # no longer reads these coords — they drive the chart dots only.
        w      = t_soft.abs().where(t_soft * mom_raw < 0, 0.0)
        capped = tau * np.tanh(mom_raw / tau)
        m_adj  = (1 - w) * mom_raw + w * capped

        sig_x = _scale(ratio_raw, ratio_win, 100.0)   # σ, 0 at the true boundary
        sig_y = _scale(m_adj,     mom_win,   0.0)      # σ, 0 at the true boundary

        df = pd.DataFrame({
            "sig_ratio":  100 + sig_x,
            "sig_mom":    100 + sig_y,
            "disp_ratio": 100 + sig_x * DISP_GAIN_X,
            "disp_mom":   100 + sig_y * DISP_GAIN_Y,
            "t_soft":     t_soft,
            "mom_raw":    mom_raw,
        }).dropna()

        # Collapse the daily chart to one point per ISO week FIRST, so the
        # wave/Fib engine measures swings on the WEEKLY cadence — golden-pocket
        # levels must come from meaningful weekly swings, not 4-day-bar noise.
        # (The volatility-scaled ZigZag adapts the swing size to each TF's σ.)
        if interval == "1d" and not df.empty:
            iso = df.index.strftime("%G-%V")
            df  = df.groupby(iso, sort=False).tail(1)
        if df.empty:
            continue

        # Wave/Fib + RSI-divergence features on the weekly-cadence RS aligned to
        # the chart points (no-lookahead; `close[t]` feeds the price-RSI layer).
        wk_rs   = rs.reindex(df.index)
        wk_rs_s = wk_rs.ewm(span=RS_SMOOTH, adjust=False).mean()
        df = df.join(wave_eng._wave_features(wk_rs_s, params, price=close[t].reindex(df.index)))
        out[t] = df

    # Cross-timeframe RSI-divergence confluence — only on the real-fetch path
    # (themes inject a synthetic panel and have no other intervals to pull).
    if fetched and out:
        try:
            _attach_mtf(out, list(out), benchmark, interval, params)
        except Exception:
            pass

    bench_idx = bench.dropna().index
    date = bench_idx[-1].strftime("%Y-%m-%d") if len(bench_idx) else None
    return out, date, close


def evaluate_tail(window, params=None, flag_wr=None, regime=None, vol_exh=None,
                  rotation=None, vol_profile=None, accum_read=None):
    """Compute the quadrant + Elliott-wave call/scores for one tail window. The
    quadrant/heading/accum/distrib still come from the SIGNAL coords (chart
    geometry + call-card ranking); the call itself is driven by the wave/Fib
    state read off the head row's precomputed wave features.

    `flag_wr` ({"bull","bull_n","bear","bear_n"}), `regime` (HEALTHY/NEUTRAL/
    DETERIORATING), `vol_exh` ("buyer"/"seller"), `vol_profile` (a confluence
    `volume_profile.current` read), and `accum_read` (an `accumulation.current`
    read) are LIVE-only conviction refinements (per-symbol
    stats have lookahead in a backtest). `rotation` ("on"/"off", the RSP/SPY regime)
    is no-lookahead, so it's passed in BOTH live and the backtest to gate entries in
    a concentration regime. All default None."""
    ratios  = [round(v, 3) for v in window["sig_ratio"].tolist()]
    moments = [round(v, 3) for v in window["sig_mom"].tolist()]

    quadrant = _quadrant(ratios[-1], moments[-1])
    accum    = _accum(ratios, moments)
    distrib  = _distrib(ratios, moments)
    strength = _tail_strength(ratios, moments)
    arrow, heading, angle = _tail_heading(ratios, moments)

    head = window.iloc[-1]
    ws = {
        "trend":      head.get("wv_trend", "none"),
        "leg_kind":   head.get("wv_leg", "none"),
        "wave":       head.get("wave_label", "—"),
        "retrace":    head.get("retrace_pct", np.nan),
        "ext":        head.get("ext_ratio", np.nan),
        "fib_target": head.get("fib_target", np.nan),
        "fib_w1":     head.get("fib_w1", np.nan),
        "gp_q":       head.get("gp_q", 0.0),
        "ext_q":      head.get("ext_q", 0.0),
        "cur":        head.get("cur_rs", np.nan),
        "abc_type":   head.get("abc_type", "—"),
        "c_tgt_lo":   head.get("c_tgt_lo", np.nan),
        "c_tgt_hi":   head.get("c_tgt_hi", np.nan),
        "w4_zone":    head.get("w4_zone", np.nan),
        "div_rs":     head.get("div_rs", "none"),
        "div_px":     head.get("div_px", "none"),
        "htf_div":    head.get("htf_div", "none"),
        "ltf_div":    head.get("ltf_div", "none"),
        "mtf_1mo":    head.get("mtf_1mo", 0.0),
        "mtf_1d":     head.get("mtf_1d", 0.0),
        "mtf_1h":     head.get("mtf_1h", 0.0),
        "flag":       head.get("flag", 0.0),
        # live-only conviction refinements (None in the backtest)
        "flag_win_bull": (flag_wr or {}).get("bull"),
        "flag_n_bull":   (flag_wr or {}).get("bull_n", 0),
        "flag_win_bear": (flag_wr or {}).get("bear"),
        "flag_n_bear":   (flag_wr or {}).get("bear_n", 0),
        "regime":     regime,
        "vol_exh":    vol_exh,
        "rotation":   rotation,
        "vol_profile": vol_profile,
        "accumulation": accum_read,   # A/D read dict (NOT the _accum ranking score below)
    }
    conviction, factors = _conviction(ws, params)
    ws["_score"] = conviction                 # avoid recomputing inside the call
    call, why = _rotation_call(ws, params)
    return {
        "conviction":     conviction,
        "conviction_factors": factors,
        "quadrant":      quadrant,
        "phase":         ws["wave"],          # tooltip "Phase" line = wave label
        "wave_label":    ws["wave"],
        "trend":         ws["trend"],
        "leg_kind":      ws["leg_kind"],
        # only a real corrective setup has a meaningful retrace (never the 480%
        # "retrace" of a noise swing, which carries wave_label "—")
        "retrace_pct":   _jsafe(ws["retrace"]) if ws["wave"] in ("wave-2", "wave-4") else None,
        "ext_ratio":     _jsafe(ws["ext"]),
        "fib_target":    _jsafe(ws["fib_target"]),
        "abc_type":      None if ws["abc_type"] == "—" else ws["abc_type"],
        "c_target_lo":   _jsafe(ws["c_tgt_lo"]),
        "c_target_hi":   _jsafe(ws["c_tgt_hi"]),
        "div_rs":        None if ws["div_rs"] == "none" else ws["div_rs"],
        "div_px":        None if ws["div_px"] == "none" else ws["div_px"],
        "div_confluence": _div_confluence(ws),
        "regime":        regime,
        "vol_exh":       vol_exh,
        "rotation":      rotation,
        # volume-profile structure (None in the backtest / themes — live-only)
        "vp_shape":      (vol_profile or {}).get("shape"),
        "vp_zone":       (vol_profile or {}).get("zone"),
        "poc":           (vol_profile or {}).get("poc"),
        "vah":           (vol_profile or {}).get("vah"),
        "val":           (vol_profile or {}).get("val"),
        # accumulation/distribution (None in the backtest / themes — live-only)
        "ad_rating":     (accum_read or {}).get("rating"),
        "ud_ratio":      (accum_read or {}).get("ud_ratio"),
        "ad_divergence": (accum_read or {}).get("divergence"),
        "dir":           _direction(ratios, moments),
        "accum":         accum,
        "distrib":       distrib,
        "strength":      strength,
        "heading":       heading,
        "heading_arrow": arrow,
        "heading_angle": angle,
        "call":          call,
        "call_why":      why,
    }


def replay_calls(series, tail, params=None, rotation=None):
    """Walk every tail window over history → {ticker: {date: {call, quadrant,
    phase}}}. The backtest's source of signal events. Cheap: ~150 dates × 11
    tickers × `tail`. `rotation` (a per-date 'on'/'off' Series) gates entries by
    the rotation regime as-of each window's head bar — no-lookahead via `.asof`."""
    out = {}
    for tk, df in series.items():
        timeline = {}
        n = len(df)
        for end in range(tail - 1, n):
            window = df.iloc[end - tail + 1:end + 1]
            rot = None
            if rotation is not None and len(rotation):
                r = rotation.asof(df.index[end])     # last known regime ≤ head date
                rot = r if isinstance(r, str) else None
            ev = evaluate_tail(window, params, rotation=rot)
            timeline[df.index[end]] = {
                "call":       ev["call"],
                "conviction": ev["conviction"],
                "quadrant":   ev["quadrant"],
                "phase":      ev["phase"],
            }
        out[tk] = timeline
    return out
