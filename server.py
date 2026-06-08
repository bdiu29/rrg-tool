#!/usr/bin/env python3
"""
Relative Rotation Graph (RRG) — local server.

Fetches price data via yfinance, computes JdK RS-Ratio and RS-Momentum
relative to a benchmark, and serves an interactive RRG chart at localhost.

Usage:
    python3 server.py
Then open http://localhost:8000 in your browser.
"""

import json
import os
import math
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    raise SystemExit(
        "Missing dependencies. Run:\n"
        "    pip3 install yfinance pandas numpy\n"
    )

# ---------------------------------------------------------------------------
# RRG math
# ---------------------------------------------------------------------------

# The 11 State Street SPDR Select Sector ETFs.
DEFAULT_TICKERS = [
    "XLK",  # Technology
    "XLE",  # Energy
    "XLV",  # Health Care
    "XLF",  # Financials
    "XLY",  # Consumer Discretionary
    "XLP",  # Consumer Staples
    "XLI",  # Industrials
    "XLB",  # Materials
    "XLU",  # Utilities
    "XLRE", # Real Estate
    "XLC",  # Communication Services
]

SECTOR_NAMES = {
    "XLK": "Technology", "XLE": "Energy", "XLV": "Health Care",
    "XLF": "Financials", "XLY": "Cons. Discretionary", "XLP": "Cons. Staples",
    "XLI": "Industrials", "XLB": "Materials", "XLU": "Utilities",
    "XLRE": "Real Estate", "XLC": "Communication",
}

BENCHMARK = "SPY"

# How far the normalized values are stretched from 100 for display. Raw z-scores
# sit tightly around 100; these spread the cloud out to fill the chart frame.
RATIO_SCALE = 3.0
MOM_SCALE = 1.8


def _rolling_zscore(series, window):
    """
    Normalize to mean 100, scaled by a rolling standard deviation.

    The window here is intentionally a FLAT (equal-weight) window, not an
    exponentially-weighted one. We tried EWMA weighting so recent bars counted
    more, but it backfired: an EW mean hugs the latest values, so every point
    ends up sitting right on top of its own average, which crushes the whole
    spread toward 100 and tangles the chart. A flat window keeps a stable
    reference, so sectors spread out across the plot the way they should.

    Recent-weighting still happens upstream, in the EMA smoothing of the
    relative-strength and momentum series — that's the right place for it
    (it speeds up the head without collapsing the scale).
    """
    mean = series.rolling(window=window, min_periods=window).mean()
    std = series.rolling(window=window, min_periods=window).std(ddof=0)
    return 100 + (series - mean) / std.replace(0, np.nan)


def _quadrant(ratio, momentum):
    """Classify an (RS-Ratio, RS-Momentum) point into an RRG quadrant."""
    if ratio >= 100:
        return "Leading" if momentum >= 100 else "Weakening"
    return "Improving" if momentum >= 100 else "Lagging"


def _direction(ratios, moments):
    """Arrow showing the most recent step's heading, e.g. '→↑'."""
    if len(ratios) < 2:
        return "·"
    dr, dm = ratios[-1] - ratios[-2], moments[-1] - moments[-2]
    h = "→" if dr > 0.02 else ("←" if dr < -0.02 else "·")
    v = "↑" if dm > 0.02 else ("↓" if dm < -0.02 else "·")
    return h + v


<<<<<<< HEAD
def _accum(ratios, moments):
    """
    Accumulation score (0-100). Favors sectors turning UP early in the cycle:
    rising momentum, a fresh upward kick, and room below the benchmark (RS<100)
    so we catch rotation before it's already extended into Leading.
=======
# Compass step (Δratio, Δmomentum) → 8-point arrow. RS on x, momentum on y.
_COMPASS = [
    (1, 0, "→"), (1, 1, "↗"), (0, 1, "↑"), (-1, 1, "↖"),
    (-1, 0, "←"), (-1, -1, "↙"), (0, -1, "↓"), (1, -1, "↘"),
]


def _tail_heading(ratios, moments):
    """
    Overall direction of travel across the WHOLE tail, not just the last bar.

    The notes treat tail direction as the core read ("northeast = strongest,
    southwest = weakest") and stress it should be *sustained* over several
    periods, so a single-bar arrow is too noisy. We fit the net displacement
    from the tail's first point to its last, then snap it to an 8-point compass.

    Returns (arrow, heading_label, angle_degrees). Angle is math-convention
    (0°=east/right, 90°=north/up). 'flat' when the move is negligible.
    """
    if len(ratios) < 2:
        return "·", "flat", None
    dr = ratios[-1] - ratios[0]      # net RS travel over the tail
    dm = moments[-1] - moments[0]    # net momentum travel over the tail
    mag = math.hypot(dr, dm)
    if mag < 0.4:                    # below this, there's no meaningful heading
        return "·", "flat", None

    angle = math.degrees(math.atan2(dm, dr)) % 360
    # Snap to nearest 45° compass point.
    idx = int((angle + 22.5) // 45) % 8
    arrow = _COMPASS[idx][2]
    label = {
        "→": "E", "↗": "NE", "↑": "N", "↖": "NW",
        "←": "W", "↙": "SW", "↓": "S", "↘": "SE",
    }[arrow]
    return arrow, label, round(angle, 1)


def _tail_strength(ratios, moments):
    """
    How far the sector has travelled across the tail (0-100). The notes list
    tail length as its own checklist item: a longer tail = a stronger, more
    committed move; a stubby tail = little relative change worth acting on.

    We sum the per-step path length (not just endpoint distance, so a curving
    but committed move still scores), then map it onto 0-100 with a soft cap.
    """
    if len(ratios) < 2:
        return 0
    path = 0.0
    for i in range(1, len(ratios)):
        path += math.hypot(ratios[i] - ratios[i - 1], moments[i] - moments[i - 1])
    # ~8 units of cumulative travel reads as a full-strength move on the scaled
    # frame; clamp so very long tails don't blow past 100.
    return int(max(0, min(100, round(path / 8.0 * 100))))


def _accum(ratios, moments):
    """
    Accumulation (rotate-IN) score (0-100). Favors sectors turning UP early in
    the cycle: rising momentum, a fresh upward kick, and room below the
    benchmark (RS<100) so we catch rotation before it's already extended into
    Leading. Embodies the notes' "momentum turns first, RS turns second".
>>>>>>> d353f6a (added rotation calls)
    """
    if len(ratios) < 2:
        return 0
    mom_slope = moments[-1] - moments[0]            # momentum trend over the tail
    kick = max(0.0, moments[-1] - moments[-2])      # most recent upward push
    room = min(max(0.0, 100 - ratios[-1]), 12.0)    # upside room below SPY
<<<<<<< HEAD
    # Coefficients tuned for the scaled value spread (~±10 ratio, ±5 momentum).
    raw = 50 + 3 * mom_slope + 1.5 * kick + 2 * room
    return int(max(0, min(100, round(raw))))


=======
    # Coefficients match the README formula (50 + 6·slope + 3·kick + 2·room),
    # tuned for the scaled value spread (~±10 ratio, ±5 momentum).
    raw = 50 + 6 * mom_slope + 3 * kick + 2 * room
    return int(max(0, min(100, round(raw))))


def _distrib(ratios, moments):
    """
    Distribution (rotate-OUT) score (0-100). The mirror image of _accum: it
    flags sectors rolling OVER — the exit side the notes weight as heavily as
    entry ("reduce exposure once a sector enters Weakening", "tail points
    southwest", "momentum deteriorates for several periods").

    High when momentum is falling, there's a fresh downward kick, and the
    sector is extended above the benchmark (RS>100) so there's leadership to
    give back. Symmetric with _accum by construction.
    """
    if len(ratios) < 2:
        return 0
    mom_slope = moments[0] - moments[-1]            # momentum DETERIORATION over tail
    kick = max(0.0, moments[-2] - moments[-1])      # most recent downward push
    extended = min(max(0.0, ratios[-1] - 100), 12.0)  # leadership above SPY to lose
    raw = 50 + 6 * mom_slope + 3 * kick + 2 * extended
    return int(max(0, min(100, round(raw))))


def _rotation_call(ratios, moments, quadrant, accum, distrib, strength, heading_label):
    """
    Turn the full picture into a single actionable call:
      ROTATE IN  — early/confirmed upside rotation worth adding
      ROTATE OUT — leadership rolling over; trim/exit
      HOLD       — strong and still rising; stay
      AVOID      — weak with no upturn yet; stand aside
      WATCH      — mixed/low-conviction; not enough to act

    The logic follows the notes' decision rules rather than pure quadrant
    location: it's the *direction of travel* and momentum, confirmed by a tail
    with enough length to mean something, that drives the call. A stubby tail
    (little relative change) is downgraded to WATCH regardless of quadrant.
    """
    rising = heading_label in ("NE", "N", "E")
    falling = heading_label in ("SW", "S", "W")

    # A move needs some committed travel to be trusted; otherwise just watch.
    weak_tail = strength < 18

    # A flat/stubby tail means little relative change — no conviction in any
    # direction. Don't issue a HOLD/IN/OUT off it; downgrade to WATCH. (The
    # decisive rolling-over and turning-up cases below already cleared this bar.)
    if heading_label == "flat" or weak_tail:
        if quadrant in ("Leading", "Weakening"):
            return "WATCH", "Little relative movement — tail too short to act on"
        if quadrant == "Lagging":
            return "AVOID", "Weak and going nowhere — no upturn to act on"
        return "WATCH", "Improving but tail is short — wait for a committed turn"

    if quadrant == "Improving":
        if rising:
            return "ROTATE IN", "Improving + heading NE — early upside, momentum has turned"
        return "WATCH", "Improving but not yet heading up — wait for a committed turn"

    if quadrant == "Leading":
        if falling or distrib >= 62:
            return "ROTATE OUT", "Leading but rolling over — momentum fading, trim into strength"
        return "HOLD", "Leading and still rising — stay with the leader"

    if quadrant == "Weakening":
        # The notes: many reduce exposure on *entering* Weakening, not Lagging.
        if rising and accum >= 60:
            return "WATCH", "Weakening but curling back up — possible re-acceleration"
        return "ROTATE OUT", "Weakening — outperforming but momentum gone, reduce exposure"

    # Lagging
    if rising:
        return "ROTATE IN", "Lagging but turning up — earliest signal, momentum leading RS"
    return "AVOID", "Lagging and not turning — weak with no upturn, stand aside"


>>>>>>> d353f6a (added rotation calls)
def compute_rrg(tickers, benchmark, interval, tail=6, rs_window=14, mom_window=14, smooth=5):
    """
    Returns a dict of {ticker: {name, ratio:[...], momentum:[...], dates:[...]}}
    giving the last `tail` points of the RS-Ratio / RS-Momentum trajectory.
    """
    # Pull enough history to warm up the rolling windows plus the tail.
    period = "2y" if interval == "1wk" else "6mo"
    symbols = tickers + [benchmark]

    raw = yf.download(
        symbols, period=period, interval=interval,
        auto_adjust=True, progress=False, group_by="column",
    )

    # yfinance returns a multi-index frame; grab adjusted close.
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw.to_frame()

    close = close.dropna(how="all").ffill()
    bench = close[benchmark]

    # Benchmark's own latest-period return, so we can express each sector
    # relative to SPY ("TODAY VS SPY").
    bench_prices = bench.dropna()
    bench_ret = None
    if len(bench_prices) >= 2:
        bench_ret = (bench_prices.iloc[-1] / bench_prices.iloc[-2] - 1) * 100

    results = {}
    for t in tickers:
        if t not in close.columns or close[t].dropna().empty:
            continue

        # Relative strength vs benchmark, indexed to 100.
        rs = (close[t] / bench) * 100

        # JdK RS-Ratio: lightly smoothed, normalized relative strength.
        rs_s = rs.ewm(span=smooth, adjust=False).mean()
        rs_ratio = _rolling_zscore(rs_s, rs_window)

        # JdK RS-Momentum: smoothed rate of change of the ratio, normalized.
        # The raw 1-period diff is noisy, so we EMA-smooth it before scaling —
        # this is what turns jagged tails into clean curves.
        mom_raw = rs_ratio.diff().ewm(span=smooth, adjust=False).mean()
        rs_mom = _rolling_zscore(mom_raw, mom_window)

        # Spread the normalized values out so they fill the chart like his.
        # A raw z-score clusters tightly around 100 (±2-3); these multipliers
        # set the axis "units" so sectors separate visibly. Tune RATIO_SCALE /
        # MOM_SCALE to make the cloud bigger or smaller.
        rs_ratio = 100 + (rs_ratio - 100) * RATIO_SCALE
        rs_mom = 100 + (rs_mom - 100) * MOM_SCALE

        df = pd.DataFrame({"ratio": rs_ratio, "momentum": rs_mom}).dropna()
        if df.empty:
            continue

        df = df.tail(tail)
        ratios = [round(v, 3) for v in df["ratio"].tolist()]
        moments = [round(v, 3) for v in df["momentum"].tolist()]

        # Latest-period price change (%), and the same figure relative to SPY.
        prices = close[t].dropna()
        change_pct = rel_pct = None
        if len(prices) >= 2:
            change_pct = round((prices.iloc[-1] / prices.iloc[-2] - 1) * 100, 2)
            if bench_ret is not None:
                rel_pct = round(change_pct - bench_ret, 2)

<<<<<<< HEAD
=======
        quadrant = _quadrant(ratios[-1], moments[-1])
        accum = _accum(ratios, moments)
        distrib = _distrib(ratios, moments)
        strength = _tail_strength(ratios, moments)
        arrow, heading, angle = _tail_heading(ratios, moments)
        call, rationale = _rotation_call(
            ratios, moments, quadrant, accum, distrib, strength, heading
        )

>>>>>>> d353f6a (added rotation calls)
        results[t] = {
            "name": SECTOR_NAMES.get(t, t),
            "ratio": ratios,
            "momentum": moments,
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "change_pct": change_pct,
            "rel_pct": rel_pct,
<<<<<<< HEAD
            "quadrant": _quadrant(ratios[-1], moments[-1]),
            "dir": _direction(ratios, moments),
            "accum": _accum(ratios, moments),
=======
            "quadrant": quadrant,
            "dir": _direction(ratios, moments),
            "accum": accum,
            "distrib": distrib,
            "strength": strength,           # tail length / move conviction (0-100)
            "heading": heading,             # NE/SE/SW/NW/... overall tail direction
            "heading_arrow": arrow,
            "heading_angle": angle,
            "call": call,                   # ROTATE IN / OUT / HOLD / AVOID / WATCH
            "call_why": rationale,
>>>>>>> d353f6a (added rotation calls)
        }

    # "Best setup" = highest accumulation score: a sector rotating UP early,
    # not one already extended into Leading.
    best = None
    for t, d in results.items():
        if best is None or d["accum"] > best["accum"]:
            best = {
                "ticker": t,
                "name": d["name"],
                "ratio": d["ratio"][-1],
                "momentum": d["momentum"][-1],
                "quadrant": d["quadrant"],
                "dir": d["dir"],
                "accum": d["accum"],
            }

    return {"sectors": results, "best": best, "date": bench_prices.index[-1].strftime("%Y-%m-%d") if len(bench_prices) else None}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet logs

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "/index.html":
            with open(os.path.join(HERE, "index.html"), "r") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/rrg":
            qs = parse_qs(parsed.query)
            timeframe = qs.get("timeframe", ["weekly"])[0]
            interval = "1wk" if timeframe == "weekly" else "1d"
            tail = int(qs.get("tail", ["6"])[0])
            tail = max(3, min(tail, 14))  # clamp to a sane range

            try:
                result = compute_rrg(DEFAULT_TICKERS, BENCHMARK, interval, tail)
                payload = {
                    "benchmark": BENCHMARK,
                    "timeframe": timeframe,
                    "tail": tail,
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date": result.get("date"),
                    "sectors": result["sectors"],
                    "best": result["best"],
                }
                self._send(200, json.dumps(payload))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
            return

        self._send(404, json.dumps({"error": "not found"}))


def main():
    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"\n  RRG tool running →  http://localhost:{port}\n")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()