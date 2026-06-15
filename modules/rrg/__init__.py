"""
RRG module — Relative Rotation Graph for SPDR sector ETFs.

The math lives in `signal.py` (decoupled signal-space vs display-space) and the
strategy validation in `backtest.py`. This file is the thin orchestration:
shape the chart JSON and wire the routes.

Routes registered:
  GET  /rrg.html      → index.html
  GET  /index.html    → index.html (legacy alias)
  GET  /api/rrg       → JSON sector rotation data
  POST /api/rrg/backtest → walk-forward validation of the rotation calls
"""

from datetime import datetime
from pathlib import Path

try:
    import yfinance as yf      # noqa: F401  (import-time dependency check)
    import pandas as pd        # noqa: F401
    import numpy as np         # noqa: F401
except ImportError:
    raise SystemExit(
        "Missing dependencies. Run:\n"
        "    pip3 install yfinance pandas numpy\n"
    )

from modules import Response
from . import backtest, signal
from .signal import (        # re-exported for schwab (intentional cross-module use)
    DEFAULT_TICKERS,
    SECTOR_NAMES,
    BENCHMARK,
    WARN_CALLS,
)

_MODULE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Chart assembly
# ---------------------------------------------------------------------------

def compute_rrg(tickers, benchmark, interval, tail=6, asof=None, close=None):
    """Returns {sectors: {ticker: {...}}, best: {...}, date: "YYYY-MM-DD"}.

    Display coords (`ratio`/`momentum`) drive the chart; the quadrant, phase,
    rotation call and scores are computed from SIGNAL coords in `signal.py`, so
    the dots you see and the calls you act on share one boundary at 100.

    `asof` ("YYYY-MM-DD") truncates history so the chart shows the RRG as of a
    past date. All windows are trailing, so rollback only removes head points.

    `close` lets a caller inject a pre-built close panel (themes module → RRG of
    synthetic theme indices); when None, `compute_series` fetches prices.
    """
    injected = close is not None     # themes pass a synthetic panel (no real volume)
    series, date, close = signal.compute_series(tickers, benchmark, interval,
                                                asof=asof, close=close)

    # Live-only conviction refinements: current regime + per-symbol flag edge +
    # volume exhaustion (real sector ETFs only — synthetic theme indices have no
    # volume). All fail-soft: the chart must render even if breadth/yfinance hiccup.
    regime, wr_map, exh_map = None, {}, {}
    if not injected:
        try:
            regime = signal.current_regime()
        except Exception:
            regime = None
        try:
            wr_map = signal.flag_win_rates_for(list(series))
        except Exception:
            wr_map = {}
        try:
            exh_map = signal.exhaustion_for(list(series))
        except Exception:
            exh_map = {}

    bench = close[benchmark]
    bench_prices = bench.dropna()
    bench_ret = None
    if len(bench_prices) >= 2:
        bench_ret = (bench_prices.iloc[-1] / bench_prices.iloc[-2] - 1) * 100

    results = {}
    for t, df in series.items():
        win = df.tail(tail)
        if win.empty:
            continue
        ev = signal.evaluate_tail(win, flag_wr=wr_map.get(t), regime=regime,
                                  vol_exh=exh_map.get(t))

        prices     = close[t].dropna()
        change_pct = rel_pct = None
        if len(prices) >= 2:
            change_pct = round((prices.iloc[-1] / prices.iloc[-2] - 1) * 100, 2)
            if bench_ret is not None:
                rel_pct = round(change_pct - bench_ret, 2)

        results[t] = {
            "name":          SECTOR_NAMES.get(t, t),
            "ratio":         [round(v, 3) for v in win["disp_ratio"].tolist()],
            "momentum":      [round(v, 3) for v in win["disp_mom"].tolist()],
            "dates":         [d.strftime("%Y-%m-%d") for d in win.index],
            "change_pct":    change_pct,
            "rel_pct":       rel_pct,
            **ev,
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

    return {"sectors": results, "best": best, "date": date}


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_index(req):
    with open(_MODULE_DIR / "index.html") as f:
        return Response.html(f.read())


def _handle_rrg(req):
    timeframe = req.qs.get("timeframe", ["daily"])[0]
    interval  = "1wk" if timeframe == "weekly" else "1d"
    tail      = int(req.qs.get("tail", ["6"])[0])
    tail      = max(3, min(tail, 14))
    asof      = req.qs.get("asof", [""])[0].strip() or None
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


def _handle_backtest(req):
    try:
        body = req.json_body() or {}
    except Exception:
        body = {}
    timeframe = body.get("timeframe", "daily")
    interval  = "1wk" if timeframe == "weekly" else "1d"
    tail      = max(3, min(int(body.get("tail", 6)), 14))
    exit_cfg  = body.get("exit") or {}
    universe  = body.get("universe", backtest.DEFAULT_UNIVERSE)
    benchmark = body.get("benchmark", backtest.DEFAULT_BENCHMARK)
    try:
        if body.get("walk_forward"):
            report = backtest.walk_forward_search(interval=interval, tail=tail,
                                                  exit_cfg=exit_cfg, universe=universe,
                                                  benchmark=benchmark)
        else:
            report = backtest.run_backtest(interval=interval, tail=tail,
                                           exit_cfg=exit_cfg, universe=universe,
                                           benchmark=benchmark)
        report["timeframe"] = timeframe
        return Response.json(report)
    except Exception as e:
        return Response.error(f"backtest failed: {e}", 500)


def register_routes(router):
    router.get("/rrg.html",        _handle_index)
    router.get("/index.html",      _handle_index)   # legacy alias
    router.get("/api/rrg",         _handle_rrg)
    router.post("/api/rrg/backtest", _handle_backtest)
