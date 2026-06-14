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

import math
import time

import yfinance as yf
import pandas as pd
import numpy as np

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

# Baked from a 4-fold walk-forward search (backtest.walk_forward_search) on the
# 11 SPDR sectors / 3y: the combo with the best mean out-of-sample IN−OUT excess
# separation at +10d (≈+1.7%, positive in 3 of 4 folds). Re-run that search to
# recalibrate against forward returns — this replaces the deleted image fit.
DEFAULTS = {
    "TREND_ETA":   1.00,   # tanh scale for the soft trend direction
    "MOM_TAU":     0.11,   # EW corrective cap, in raw-momentum units
    "DIRECT_GATE": 0.60,   # net/path below this = corrective chop → no ROTATE (scale-free)
    "MOVE_GATE":   0.90,   # min net tail displacement for a leg to count, in σ
    "DIV_MARGIN":  0.40,   # momentum fall below its tail peak to flag wave-5 roll-over, in σ
}


def _p(params, key):
    return (params or {}).get(key, DEFAULTS[key])


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


def _wave_phase(trend, mom):
    """Elliott-wave phase of the head bar from the soft trend direction and raw
    momentum. |trend| < 0.25 = the higher-degree trend itself is turning."""
    if trend is None or mom is None or not (np.isfinite(trend) and np.isfinite(mom)):
        return "—"
    if abs(trend) < 0.25:
        return "basing (trend turn)" if mom >= 0 else "topping (trend turn)"
    if trend > 0:
        return "impulse ↑ (wave 3/5)" if mom >= 0 else "pullback (wave 2/4)"
    return "impulse ↓ (wave C/3)" if mom < 0 else "bounce (wave B)"


def _rotation_call(ratios, moments, quadrant, accum, distrib, heading_label,
                   phase="—", params=None):
    """Translate the tail into one call: ROTATE IN / ROTATE OUT / HOLD / AVOID /
    WATCH. Reads the tail in Elliott-wave terms — a straight committed leg
    (directness ≥ DIRECT_GATE and net ≥ MOVE_GATE σ) is impulse (wave 3/5) and
    may trigger a ROTATE; a bent overlapping tail is corrective (wave 2/4) and
    never does (the whipsaw guard). Momentum DIV_MARGIN σ below its tail peak
    while RS holds its high = wave-5 exhaustion → trim."""
    if len(ratios) < 2:
        return "WATCH", "Not enough tail history to judge"

    direct_gate = _p(params, "DIRECT_GATE")
    move_gate   = _p(params, "MOVE_GATE")
    div_margin  = _p(params, "DIV_MARGIN")

    net  = math.hypot(ratios[-1] - ratios[0], moments[-1] - moments[0])
    path = sum(
        math.hypot(ratios[i] - ratios[i - 1], moments[i] - moments[i - 1])
        for i in range(1, len(ratios))
    )
    directness = net / path if path > 1e-9 else 0.0
    impulse    = directness >= direct_gate and net >= move_gate
    exhausted  = (
        ratios[-1] >= max(ratios) - 0.3
        and max(moments) - moments[-1] >= div_margin
    )

    rising   = heading_label in ("NE", "N", "E")
    falling  = heading_label in ("SW", "S", "W")
    bounce   = phase.startswith("bounce")     # corrective rally inside a downtrend
    pullback = phase.startswith("pullback")   # corrective dip inside an uptrend

    if not impulse:
        if quadrant == "Leading":
            return "HOLD",  "Leader in a corrective pause (wave-2/4 character) — trend intact, sit through it"
        if quadrant == "Weakening":
            return "WATCH", "Choppy drift in Weakening — could be a wave 4 before another leg; wait for a committed move"
        if quadrant == "Improving":
            return "WATCH", "Improving but the tail is corrective chop — no impulse to join yet"
        return     "AVOID", "Lagging with no committed leg — nothing to act on"

    if quadrant == "Improving":
        if rising and bounce:
            return "WATCH",      "Corrective bounce (wave B) — downtrend intact, not a new impulse"
        if rising:
            return "ROTATE IN",  "Committed leg up through Improving — wave-3 character, momentum leading RS"
        return     "WATCH",      "Improving but the leg points down — failed turn, stand by"

    if quadrant == "Leading":
        if exhausted:
            return "ROTATE OUT", "Wave-5 exhaustion — RS at its highs but momentum diverging; trim into strength"
        if pullback:
            return "HOLD",       "Wave-2/4 pullback in an uptrend — momentum dip, trend intact"
        if falling or distrib >= 70:
            return "ROTATE OUT", "Leading but rolling over — momentum fading, reduce before RS follows"
        return     "HOLD",       "Leading on a committed leg — ride the impulse"

    if quadrant == "Weakening":
        if pullback:
            return "WATCH",      "Wave-2/4 pullback below the line — uptrend not broken yet, watch for the next leg"
        if rising and accum >= 60:
            return "WATCH",      "Weakening but impulsing back up — possible new wave 1; confirm before adding"
        return     "ROTATE OUT", "Committed leg down out of Leading — distribution underway, reduce exposure"

    # Lagging
    if rising and bounce:
        return     "WATCH",      "Corrective bounce (wave B) inside the downtrend — wait for a real trend flip"
    if rising:
        return     "ROTATE IN",  "Committed turn up from Lagging — earliest entry, momentum turns first"
    return         "AVOID",      "Impulsing lower in Lagging — the down-leg isn't finished"


# ---------------------------------------------------------------------------
# Price data (one cached yfinance path, shared by the chart and the backtest)
# ---------------------------------------------------------------------------

_PRICE_CACHE = {}     # (interval, sorted symbols) -> (fetched_at, close_df)
_PRICE_TTL   = 600    # seconds


def _fetch_close(symbols, interval, period=PERIOD):
    key = (interval, tuple(sorted(symbols)))
    hit = _PRICE_CACHE.get(key)
    if hit and time.time() - hit[0] < _PRICE_TTL:
        return hit[1]
    raw = yf.download(
        symbols, period=period, interval=interval,
        auto_adjust=True, progress=False, group_by="column",
    )
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw.to_frame()
    close = close.dropna(how="all").ffill()
    _PRICE_CACHE[key] = (time.time(), close)
    return close


_OHLC_CACHE = {}      # (sorted symbols, period) -> (fetched_at, dict_of_field_frames)


def fetch_ohlc(symbols, period=PERIOD):
    """Daily OHLC for the backtest (next-bar-open entry + ATR exits). yfinance
    auto-adjusts all four fields consistently."""
    key = (tuple(sorted(symbols)), period)
    hit = _OHLC_CACHE.get(key)
    if hit and time.time() - hit[0] < _PRICE_TTL:
        return hit[1]
    raw = yf.download(symbols, period=period, interval="1d",
                      auto_adjust=True, progress=False, group_by="column")
    out = {}
    for field in ("Open", "High", "Low", "Close"):
        if isinstance(raw.columns, pd.MultiIndex):
            out[field.lower()] = raw[field]
        else:
            out[field.lower()] = raw[field].to_frame()
    _OHLC_CACHE[key] = (time.time(), out)
    return out


# ---------------------------------------------------------------------------
# Series + per-tail evaluation
# ---------------------------------------------------------------------------

def compute_series(tickers, benchmark, interval, asof=None, params=None):
    """Per-ticker point-cadence DataFrames with both coordinate systems.

    Returns (series, date, close) where:
      series = {ticker: DataFrame[sig_ratio, sig_mom, disp_ratio, disp_mom,
                                  t_soft, mom_raw]} at one row per tail point
               (daily → one per ISO week, anchored to the week's last bar),
      date   = the latest benchmark bar date (str) or None,
      close  = the raw close DataFrame (for change_pct / rel_pct in the chart).
    """
    fast, slow, mom_diff, mom_smooth, trend_len, ratio_win, mom_win = LENS[interval]
    eta = _p(params, "TREND_ETA")
    tau = _p(params, "MOM_TAU")

    symbols = list(tickers) + [benchmark]
    close   = _fetch_close(symbols, interval, PERIOD)
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

        # Elliott-wave squash (now on BOTH intervals): momentum that opposes the
        # prevailing trend is corrective and gets capped at the boundary.
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

        if interval == "1d" and not df.empty:
            iso = df.index.strftime("%G-%V")
            df  = df.groupby(iso, sort=False).tail(1)
        if df.empty:
            continue
        out[t] = df

    bench_idx = bench.dropna().index
    date = bench_idx[-1].strftime("%Y-%m-%d") if len(bench_idx) else None
    return out, date, close


def evaluate_tail(window, params=None):
    """Compute the quadrant/phase/call/scores for one tail window (a DataFrame
    of the last `tail` point rows). Reads SIGNAL coords — the single source of
    truth shared by the live chart and the backtest replay."""
    ratios  = [round(v, 3) for v in window["sig_ratio"].tolist()]
    moments = [round(v, 3) for v in window["sig_mom"].tolist()]

    quadrant = _quadrant(ratios[-1], moments[-1])
    accum    = _accum(ratios, moments)
    distrib  = _distrib(ratios, moments)
    strength = _tail_strength(ratios, moments)
    arrow, heading, angle = _tail_heading(ratios, moments)

    ts  = window["t_soft"].iloc[-1]
    mr  = window["mom_raw"].iloc[-1]
    phase = _wave_phase(ts, mr)

    call, why = _rotation_call(ratios, moments, quadrant, accum, distrib,
                               heading, phase, params)
    return {
        "quadrant":      quadrant,
        "phase":         phase,
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


def replay_calls(series, tail, params=None):
    """Walk every tail window over history → {ticker: {date: {call, quadrant,
    phase}}}. The backtest's source of signal events. Cheap: ~150 dates × 11
    tickers × `tail`."""
    out = {}
    for tk, df in series.items():
        timeline = {}
        n = len(df)
        for end in range(tail - 1, n):
            window = df.iloc[end - tail + 1:end + 1]
            ev = evaluate_tail(window, params)
            timeline[df.index[end]] = {
                "call":     ev["call"],
                "quadrant": ev["quadrant"],
                "phase":    ev["phase"],
            }
        out[tk] = timeline
    return out
