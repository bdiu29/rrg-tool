"""
RRG module — Relative Rotation Graph for SPDR sector ETFs.

Routes registered:
  GET /           → index.html
  GET /index.html → index.html
  GET /api/rrg    → JSON sector rotation data
"""

import math
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

# Stretch normalized z-scores to fill the chart frame.
RATIO_SCALE = 3.0
MOM_SCALE   = 1.8

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


def _rotation_call(ratios, moments, quadrant, accum, distrib, strength, heading_label):
    """
    Translate the full picture into one actionable call:
      ROTATE IN / ROTATE OUT / HOLD / AVOID / WATCH

    Decision logic follows tail direction + momentum conviction rather than
    pure quadrant location. A stubby tail (strength < 18) is downgraded to
    WATCH regardless of quadrant.
    """
    rising    = heading_label in ("NE", "N", "E")
    falling   = heading_label in ("SW", "S", "W")
    weak_tail = strength < 18

    if heading_label == "flat" or weak_tail:
        if quadrant in ("Leading", "Weakening"):
            return "WATCH",      "Little relative movement — tail too short to act on"
        if quadrant == "Lagging":
            return "AVOID",      "Weak and going nowhere — no upturn to act on"
        return     "WATCH",      "Improving but tail is short — wait for a committed turn"

    if quadrant == "Improving":
        if rising:
            return "ROTATE IN",  "Improving + heading NE — early upside, momentum has turned"
        return     "WATCH",      "Improving but not yet heading up — wait for a committed turn"

    if quadrant == "Leading":
        if falling or distrib >= 62:
            return "ROTATE OUT", "Leading but rolling over — momentum fading, trim into strength"
        return     "HOLD",       "Leading and still rising — stay with the leader"

    if quadrant == "Weakening":
        if rising and accum >= 60:
            return "WATCH",      "Weakening but curling back up — possible re-acceleration"
        return     "ROTATE OUT", "Weakening — outperforming but momentum gone, reduce exposure"

    # Lagging
    if rising:
        return     "ROTATE IN",  "Lagging but turning up — earliest signal, momentum leading RS"
    return         "AVOID",      "Lagging and not turning — weak with no upturn, stand aside"


def compute_rrg(tickers, benchmark, interval, tail=6, rs_window=14, mom_window=14, smooth=5):
    """
    Returns {sectors: {ticker: {...}}, best: {...}, date: "YYYY-MM-DD"}.
    Each ticker dict includes ratio/momentum tails, quadrant, rotation call, and scores.
    """
    period  = "2y" if interval == "1wk" else "6mo"
    symbols = tickers + [benchmark]

    raw = yf.download(
        symbols, period=period, interval=interval,
        auto_adjust=True, progress=False, group_by="column",
    )

    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw.to_frame()
    close = close.dropna(how="all").ffill()
    bench = close[benchmark]

    bench_prices = bench.dropna()
    bench_ret    = None
    if len(bench_prices) >= 2:
        bench_ret = (bench_prices.iloc[-1] / bench_prices.iloc[-2] - 1) * 100

    results = {}
    for t in tickers:
        if t not in close.columns or close[t].dropna().empty:
            continue

        rs       = (close[t] / bench) * 100
        rs_s     = rs.ewm(span=smooth, adjust=False).mean()
        rs_ratio = _rolling_zscore(rs_s, rs_window)

        mom_raw  = rs_ratio.diff().ewm(span=smooth, adjust=False).mean()
        rs_mom   = _rolling_zscore(mom_raw, mom_window)

        rs_ratio = 100 + (rs_ratio - 100) * RATIO_SCALE
        rs_mom   = 100 + (rs_mom   - 100) * MOM_SCALE

        df = pd.DataFrame({"ratio": rs_ratio, "momentum": rs_mom}).dropna().tail(tail)
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
        call, rationale = _rotation_call(
            ratios, moments, quadrant, accum, distrib, strength, heading
        )

        results[t] = {
            "name":          SECTOR_NAMES.get(t, t),
            "ratio":         ratios,
            "momentum":      moments,
            "dates":         [d.strftime("%Y-%m-%d") for d in df.index],
            "change_pct":    change_pct,
            "rel_pct":       rel_pct,
            "quadrant":      quadrant,
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
    timeframe = req.qs.get("timeframe", ["weekly"])[0]
    interval  = "1wk" if timeframe == "weekly" else "1d"
    tail      = int(req.qs.get("tail", ["6"])[0])
    tail      = max(3, min(tail, 14))
    try:
        result = compute_rrg(DEFAULT_TICKERS, BENCHMARK, interval, tail)
        return Response.json({
            "benchmark": BENCHMARK,
            "timeframe": timeframe,
            "tail":      tail,
            "updated":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "date":      result.get("date"),
            "sectors":   result["sectors"],
            "best":      result["best"],
        })
    except Exception as e:
        return Response.error(str(e))


def register_routes(router):
    router.get("/",           _handle_index)
    router.get("/index.html", _handle_index)
    router.get("/api/rrg",    _handle_rrg)
