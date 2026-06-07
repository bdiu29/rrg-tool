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


def _accum(ratios, moments):
    """
    Accumulation score (0-100). Favors sectors turning UP early in the cycle:
    rising momentum, a fresh upward kick, and room below the benchmark (RS<100)
    so we catch rotation before it's already extended into Leading.
    """
    if len(ratios) < 2:
        return 0
    mom_slope = moments[-1] - moments[0]            # momentum trend over the tail
    kick = max(0.0, moments[-1] - moments[-2])      # most recent upward push
    room = min(max(0.0, 100 - ratios[-1]), 12.0)    # upside room below SPY
    # Coefficients tuned for the scaled value spread (~±10 ratio, ±5 momentum).
    raw = 50 + 3 * mom_slope + 1.5 * kick + 2 * room
    return int(max(0, min(100, round(raw))))


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

        results[t] = {
            "name": SECTOR_NAMES.get(t, t),
            "ratio": ratios,
            "momentum": moments,
            "dates": [d.strftime("%Y-%m-%d") for d in df.index],
            "change_pct": change_pct,
            "rel_pct": rel_pct,
            "quadrant": _quadrant(ratios[-1], moments[-1]),
            "dir": _direction(ratios, moments),
            "accum": _accum(ratios, moments),
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