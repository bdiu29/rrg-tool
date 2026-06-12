"""
RRG module — Relative Rotation Graph for SPDR sector ETFs.

Routes registered:
  GET /rrg.html   → index.html
  GET /index.html → index.html (legacy alias)
  GET /api/rrg    → JSON sector rotation data
"""

import math
import time
from datetime import datetime
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    raise SystemExit(
        "Missing dependencies. Run:\n"
        "    pip3 install yfinance pandas numpy\n"
    )

from modules import Response

_MODULE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Constants
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

# Weekly: per-ticker z-scores stretched to fill the frame.
RATIO_SCALE_WK = 3.5
MOM_SCALE_WK   = 2.2
# Daily: DIRECT affine scaling of the raw trend ratio and its ROC — no z-scores.
# Per-ticker z-normalization equalizes tail travel across sectors, but the
# StockCharts daily reference (sp-rrg.png, 2026-06-09) shows differential travel
# (XLE sweeps ~6 x-units while XLU crawls ~1); one shared linear map preserves
# that. Fitted by calibrate_rrg.py (mean head error 0.75, per-point deltas ≤ ~1)
# — rerun that script to recalibrate.
RATIO_C_D = 100.550   # x offset
RATIO_K_D = 1.678     # x gain on (trend ratio − 100)
MOM_K_D   = 1.626     # y gain on the lightly-smoothed 15-day ROC

# Elliott-wave phase model (daily momentum axis). Momentum that opposes the
# prevailing trend is a corrective leg — a wave-2/4 pullback in an uptrend or a
# wave-B bounce in a downtrend — and gets squashed at the 100 line: corrections
# approach the quadrant boundary but don't cross it. Only a genuine trend flip
# (t_soft ≈ 0 then sign change = new wave 1) releases the cap, so quadrant
# crossings reflect motive→corrective alternation rather than every bounce.
TREND_D   = 60     # trading days for the soft trend direction (daily)
TREND_W   = 12     # bars for the soft trend direction (weekly, calls only)
TREND_ETA = 1.0    # tanh scale: how much trend-ratio travel = full conviction
MOM_TAU   = 0.11   # corrective cap in raw-momentum units (≈0.2 y-units after K)

# Conviction gates for the Elliott-Wave rotation logic. `directness` = net displacement /
# path length: ~1.0 for a straight, committed leg; low when the tail wanders. Tuned to fire
# on the macro impulse (wave 3) and the exhaustion (wave 5) while sitting through the
# corrective wiggles (waves 2 & 4) that whipsaw a single-bar model. Raise DIRECT_GATE to be
# even less reactive; lower it to catch turns earlier.
DIRECT_GATE = 0.52   # below this the tail is corrective chop → no directional call
MOVE_GATE   = 1.0    # min net displacement (chart units) for a leg to count as real
DIV_MARGIN  = 0.6    # momentum must fall this far below its tail peak to flag a wave-5 roll-over

# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def _rolling_zscore(series, window):
    """
    Normalize to mean 100, scaled by a rolling standard deviation.

    Uses a flat (equal-weight) window intentionally — an EWMA mean hugs the
    latest values and collapses the spread toward 100. Recent-weighting is
    applied upstream via EMA smoothing of the RS/momentum series instead.
    """
    mean = series.rolling(window=window, min_periods=window).mean()
    std  = series.rolling(window=window, min_periods=window).std(ddof=0)
    return 100 + (series - mean) / std.replace(0, np.nan)


def _jdk_ratio_signal(series, fast_span, slow_span):
    """
    JdK-style RS-Ratio: fast_EMA / slow_EMA * 100.
    Stays above 100 while the sector leads (fast > slow) — unlike flat z-score
    which mean-reverts and kills horizontal tail movement.
    """
    fast = series.ewm(span=fast_span, adjust=False).mean()
    slow = series.ewm(span=slow_span, adjust=False).mean()
    return fast / slow * 100


def _quadrant(ratio, momentum):
    if ratio >= 100:
        return "Leading" if momentum >= 100 else "Weakening"
    return "Improving" if momentum >= 100 else "Lagging"


def _direction(ratios, moments):
    if len(ratios) < 2:
        return "·"
    dr = ratios[-1] - ratios[-2]
    dm = moments[-1] - moments[-2]
    h  = "→" if dr > 0.02 else ("←" if dr < -0.02 else "·")
    v  = "↑" if dm > 0.02 else ("↓" if dm < -0.02 else "·")
    return h + v


_COMPASS = [
    (1, 0, "→"), (1, 1, "↗"), (0, 1, "↑"), (-1, 1, "↖"),
    (-1, 0, "←"), (-1, -1, "↙"), (0, -1, "↓"), (1, -1, "↘"),
]


def _tail_heading(ratios, moments):
    """
    Overall tail direction as an 8-point compass arrow.

    Fits the net displacement from first to last tail point rather than
    using only the last bar — a single-bar arrow is too noisy for decisions.
    Returns (arrow, heading_label, angle_degrees).
    """
    if len(ratios) < 2:
        return "·", "flat", None
    dr  = ratios[-1] - ratios[0]
    dm  = moments[-1] - moments[0]
    mag = math.hypot(dr, dm)
    if mag < 0.4:
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
    """Cumulative path length mapped to 0-100 (stubby tail = low score)."""
    if len(ratios) < 2:
        return 0
    path = sum(
        math.hypot(ratios[i] - ratios[i - 1], moments[i] - moments[i - 1])
        for i in range(1, len(ratios))
    )
    return int(max(0, min(100, round(path / 8.0 * 100))))


def _accum(ratios, moments):
    """Accumulation/rotate-IN score (0-100). Embeds 'momentum turns first'."""
    if len(ratios) < 2:
        return 0
    mom_slope = moments[-1] - moments[0]
    kick      = max(0.0, moments[-1] - moments[-2])
    room      = min(max(0.0, 100 - ratios[-1]), 12.0)
    return int(max(0, min(100, round(50 + 6 * mom_slope + 3 * kick + 2 * room))))


def _distrib(ratios, moments):
    """Distribution/rotate-OUT score (0-100). Mirror image of _accum."""
    if len(ratios) < 2:
        return 0
    mom_slope = moments[0] - moments[-1]
    kick      = max(0.0, moments[-2] - moments[-1])
    extended  = min(max(0.0, ratios[-1] - 100), 12.0)
    return int(max(0, min(100, round(50 + 6 * mom_slope + 3 * kick + 2 * extended))))


def _wave_phase(trend, mom):
    """
    Classify the head bar's Elliott-wave phase from the soft trend direction
    and raw momentum. |trend| < 0.25 = the higher-degree trend itself is
    turning (potential wave 1 / wave-5 top) — the zone where quadrant
    crossings are legitimate.
    """
    if trend is None or mom is None or not (np.isfinite(trend) and np.isfinite(mom)):
        return "—"
    if abs(trend) < 0.25:
        return "basing (trend turn)" if mom >= 0 else "topping (trend turn)"
    if trend > 0:
        return "impulse ↑ (wave 3/5)" if mom >= 0 else "pullback (wave 2/4)"
    return "impulse ↓ (wave C/3)" if mom < 0 else "bounce (wave B)"


def _rotation_call(ratios, moments, quadrant, accum, distrib, heading_label, phase="—"):
    """
    Translate the full picture into one actionable call:
      ROTATE IN / ROTATE OUT / HOLD / AVOID / WATCH

    Reads the tail in Elliott-wave terms. A straight, committed leg
    (directness >= DIRECT_GATE and net move >= MOVE_GATE) has impulse
    character (wave 3 / 5) and may trigger a ROTATE call; a bent,
    overlapping tail has corrective character (wave 2 / 4) and never does —
    that's the whipsaw guard that keeps a 3-week pullback from reading as
    rotation. Momentum sitting DIV_MARGIN below its tail peak while RS holds
    its high is wave-5 exhaustion: the impulse is ending, trim into strength.
    """
    if len(ratios) < 2:
        return "WATCH", "Not enough tail history to judge"

    net  = math.hypot(ratios[-1] - ratios[0], moments[-1] - moments[0])
    path = sum(
        math.hypot(ratios[i] - ratios[i - 1], moments[i] - moments[i - 1])
        for i in range(1, len(ratios))
    )
    directness = net / path if path > 1e-9 else 0.0
    impulse    = directness >= DIRECT_GATE and net >= MOVE_GATE
    exhausted  = (
        ratios[-1] >= max(ratios) - 0.3
        and max(moments) - moments[-1] >= DIV_MARGIN
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


_PRICE_CACHE = {}     # (interval, sorted symbols) -> (fetched_at, close_df)
_PRICE_TTL   = 600    # seconds — date-stepping slices this cache instead of re-downloading


def _fetch_close(symbols, interval, period):
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


def compute_rrg(tickers, benchmark, interval, tail=6, rs_window=None, mom_window=None, smooth=None, asof=None):
    """
    Returns {sectors: {ticker: {...}}, best: {...}, date: "YYYY-MM-DD"}.
    Each ticker dict includes ratio/momentum tails, quadrant, rotation call, and scores.

    Both intervals use the JdK-style chain: RS-Ratio = z-scaled fast/slow EMA ratio of
    RS (a trend measure, so x travels with the trend), RS-Momentum = z-scaled ROC of
    that trend (y is the velocity of x, so tails arc clockwise like a real RRG). The
    slow EMA keeps the macro character — a 3-week wave-2 pullback won't flip x, a real
    trend change will.

    `asof` ("YYYY-MM-DD") truncates the price history so the chart shows the RRG as of
    a past date. All windows are trailing, so rolling back only removes head points —
    historical tail points stay put.
    """
    if interval == "1wk":
        smooth       = smooth     or 10   # fast EMA span (10 weeks)
        rs_window    = rs_window  or 40   # slow EMA span (~9 months)
        scale_window = 52                 # z-score normalization window (visual spread only)
        mom_window   = mom_window or 26   # 6-month momentum normalization
        mom_diff     = 5                  # 5-week ROC of the RS trend
        mom_smooth   = 3                  # 3-week EMA on the ROC
        trend_len    = TREND_W
        period       = "3y"
    else:
        # Daily — 50/140 trading-day macro lens, DIRECT-scaled rather than
        # z-normalized; see RATIO_K_D note above. mom_smooth is deliberately
        # LIGHT: momentum must lead the ratio for leaders to arc over the top of
        # the oval (heavy smoothing = lag = straight diagonal tails, no rollover).
        smooth       = smooth     or 50   # fast EMA span (≈10 weeks)
        rs_window    = rs_window  or 140  # slow EMA span (≈28 weeks)
        mom_diff     = 15                 # 3-week ROC of the RS trend
        mom_smooth   = 3                  # 3-day EMA on the ROC
        trend_len    = TREND_D
        period       = "3y"

    symbols = tickers + [benchmark]
    close   = _fetch_close(symbols, interval, period)
    if asof:
        close = close.loc[:asof]
        if close.empty:
            raise ValueError(f"No price data on or before {asof}")
    bench = close[benchmark]

    bench_prices = bench.dropna()
    bench_ret    = None
    if len(bench_prices) >= 2:
        bench_ret = (bench_prices.iloc[-1] / bench_prices.iloc[-2] - 1) * 100

    results = {}
    for t in tickers:
        if t not in close.columns or close[t].dropna().empty:
            continue

        rs = (close[t] / bench) * 100

        ratio_raw = _jdk_ratio_signal(rs, smooth, rs_window)
        mom_raw   = ratio_raw.diff(mom_diff).ewm(span=mom_smooth, adjust=False).mean()

        # Soft trend direction in (-1, 1) — the higher-degree wave that the EW
        # phase model measures legs against.
        t_soft = np.tanh(ratio_raw.diff(trend_len) / TREND_ETA)

        if interval == "1wk":
            rs_ratio = 100 + (_rolling_zscore(ratio_raw, scale_window) - 100) * RATIO_SCALE_WK
            rs_mom   = 100 + (_rolling_zscore(mom_raw, mom_window) - 100) * MOM_SCALE_WK
        else:
            # EW phase squash — see the MOM_TAU note above.
            w        = t_soft.abs().where(t_soft * mom_raw < 0, 0.0)
            capped   = MOM_TAU * np.tanh(mom_raw / MOM_TAU)
            m_adj    = (1 - w) * mom_raw + w * capped
            rs_ratio = RATIO_C_D + (ratio_raw - 100) * RATIO_K_D
            rs_mom   = 100 + m_adj * MOM_K_D

        df_all = pd.DataFrame({"ratio": rs_ratio, "momentum": rs_mom}).dropna()
        if interval == "1d":
            # One tail point per calendar week, anchored to each week's LAST bar —
            # not counted back from the newest bar — so historical points stay put
            # when the as-of date steps day by day. The newest (partial) week's
            # last bar is the live head.
            iso_week = df_all.index.strftime("%G-%V")
            df_all   = df_all.groupby(iso_week, sort=False).tail(1)
        df = df_all.tail(tail)
        if df.empty:
            continue

        ratios  = [round(v, 3) for v in df["ratio"].tolist()]
        moments = [round(v, 3) for v in df["momentum"].tolist()]

        prices     = close[t].dropna()
        change_pct = rel_pct = None
        if len(prices) >= 2:
            change_pct = round((prices.iloc[-1] / prices.iloc[-2] - 1) * 100, 2)
            if bench_ret is not None:
                rel_pct = round(change_pct - bench_ret, 2)

        quadrant = _quadrant(ratios[-1], moments[-1])
        accum    = _accum(ratios, moments)
        distrib  = _distrib(ratios, moments)
        strength = _tail_strength(ratios, moments)
        arrow, heading, angle = _tail_heading(ratios, moments)

        ts_head  = t_soft.reindex(df.index).iloc[-1]
        mom_head = mom_raw.reindex(df.index).iloc[-1]
        phase    = _wave_phase(ts_head, mom_head)

        call, rationale = _rotation_call(
            ratios, moments, quadrant, accum, distrib, heading, phase
        )

        results[t] = {
            "name":          SECTOR_NAMES.get(t, t),
            "ratio":         ratios,
            "momentum":      moments,
            "dates":         [d.strftime("%Y-%m-%d") for d in df.index],
            "change_pct":    change_pct,
            "rel_pct":       rel_pct,
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
            "call_why":      rationale,
        }

    best = None
    for t, d in results.items():
        if best is None or d["accum"] > best["accum"]:
            best = {
                "ticker":   t,
                "name":     d["name"],
                "ratio":    d["ratio"][-1],
                "momentum": d["momentum"][-1],
                "quadrant": d["quadrant"],
                "dir":      d["dir"],
                "accum":    d["accum"],
            }

    return {
        "sectors": results,
        "best":    best,
        "date":    bench_prices.index[-1].strftime("%Y-%m-%d") if len(bench_prices) else None,
    }


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_index(req):
    with open(_MODULE_DIR / "index.html") as f:
        return Response.html(f.read())


def _handle_rrg(req):
    timeframe = req.qs.get("timeframe", ["daily"])[0]
    interval = "1wk" if timeframe == "weekly" else "1d"
    tail     = int(req.qs.get("tail", ["6"])[0])
    tail     = max(3, min(tail, 14))
    asof     = req.qs.get("asof", [""])[0].strip() or None
    if asof:
        try:
            datetime.strptime(asof, "%Y-%m-%d")
        except ValueError:
            return Response.error("asof must be YYYY-MM-DD", status=400)
    try:
        result = compute_rrg(DEFAULT_TICKERS, BENCHMARK, interval, tail, asof=asof)
        return Response.json({
            "benchmark": BENCHMARK,
            "timeframe": timeframe,
            "tail":      tail,
            "asof":      asof,
            "updated":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date":      result.get("date"),
            "sectors":   result["sectors"],
            "best":      result["best"],
        })
    except Exception as e:
        return Response.error(str(e))


def register_routes(router):
    router.get("/rrg.html",   _handle_index)
    router.get("/index.html", _handle_index)   # legacy alias
    router.get("/api/rrg",    _handle_rrg)
