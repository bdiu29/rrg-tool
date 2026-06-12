"""
Breadth module — market breadth tracker.

Answers one question continuously: is the broad market participating in a
move, or is it being carried by a handful of large stocks? Short-term panels
(McClellan, TRIN, % above 20/50d) drive timing; long-term panels (Summation,
A-D lines, % above 200d) set a regime that colors how the short-term signals
are read.

Routes registered:
  GET  /breadth.html           → dashboard page
  GET  /api/breadth/universes  → universe config + local-cache status
  GET  /api/breadth/dashboard  → series + regime + divergences (?universe=&days=)
  POST /api/breadth/sync       → start background backfill/update {universe, source?}
  GET  /api/breadth/progress   → sync job status
  GET  /api/breadth/summary    → compact daily summary (?universe=)

Data caveat (do not skip): universes are built from TODAY'S constituent
lists, so deep historical breadth is survivorship-biased — delisted and
removed losers are missing. Membership is stored dated so point-in-time
lists can be imported later.
"""

import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from modules import Response

from . import backfill, indicators, regime, store, universes

_MODULE_DIR = Path(__file__).resolve().parent

SURVIVORSHIP_NOTE = (
    "Universe membership is today's constituent list — deep historical "
    "breadth is survivorship-biased (delisted losers missing). Recent "
    "readings are reliable; treat multi-year history as approximate."
)

DEFAULT_UNIVERSE = "sp500"
DEFAULT_DAYS     = 504    # ~2 trading years displayed by default


def _clean(values):
    """pandas → JSON-safe list (NaN → None)."""
    out = []
    for v in values:
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            out.append(None)
        else:
            out.append(round(float(v), 4))
    return out


# ---------------------------------------------------------------------------
# Series assembly
# ---------------------------------------------------------------------------

def _full_series(universe_key):
    """(agg, der, index_close) over full stored history, aligned on agg dates."""
    cfg = universes.load_config()
    uni = cfg["universes"][universe_key]
    agg = store.get_breadth_daily(universe_key)
    der = store.get_indicator_values(universe_key)
    if agg.empty or der.empty:
        return None, None, None
    der = der.reindex(agg.index)
    idx = store.get_series(uni["index"]["symbol"])
    index_close = idx["close"].reindex(agg.index) if not idx.empty else pd.Series(
        np.nan, index=agg.index)
    return agg, der, index_close


def _analyze(agg, der, index_close):
    """Divergences, regime, ZBT, interpretation — shared by dashboard + summary."""
    events = regime.divergences(index_close, {
        "A-D line":     der["ad_line"],
        "% above 50d":  agg["pct_above_50"],
        "% above 20d":  agg["pct_above_20"],
    })
    active = regime.active_divergences(events, list(agg.index))
    reg    = regime.regime_state(der["summation"], agg["pct_above_200"],
                                 active_divergences=len(active))
    zbt        = [str(d) for d in indicators.zbt_events(der["zbt_ema"])]
    recent_zbt = [d for d in zbt if d in set(agg.index[-regime.ZBT_RECENT_BARS:])]
    latest = {
        "mcclellan":    der["mcclellan"].iloc[-1],
        "trin":         der["trin"].iloc[-1],
        "pct_above_20": agg["pct_above_20"].iloc[-1],
    }
    interp = regime.interpret(reg["state"], latest,
                              zbt_recent=recent_zbt[-1] if recent_zbt else None)
    return events, active, reg, zbt, interp


def build_dashboard(universe_key, days=DEFAULT_DAYS):
    cfg = universes.load_config()
    uni = cfg["universes"][universe_key]
    agg, der, index_close = _full_series(universe_key)
    status = store.universe_status(universe_key)
    if agg is None:
        return {"universe": universe_key, "name": uni["name"], "empty": True,
                "status": status, "note": SURVIVORSHIP_NOTE}

    events, active, reg, zbt, interp = _analyze(agg, der, index_close)

    # Concentration gauge (RSP vs SPY): cap-weight vs equal-weight proxy.
    conc, conc_label = None, None
    cc = cfg.get("concentration", {})
    if cc.get("numerator") and cc.get("denominator"):
        num = store.get_series(cc["numerator"])
        den = store.get_series(cc["denominator"])
        if not num.empty and not den.empty:
            ratio      = (num["close"] / den["close"]).reindex(agg.index)
            conc       = ratio
            conc_label = f"{cc['numerator']} / {cc['denominator']}"

    window = agg.index[-days:]
    in_win = set(window)
    a, d, ic = agg.loc[window], der.loc[window], index_close.loc[window]

    series = {k: _clean(d[k]) for k in
              ("mcclellan", "summation", "trin", "ad_line", "ad_vol_line",
               "net_up_vol", "ud_vol_ratio", "nh_nl", "hl_index")}
    series.update({k: _clean(a[k]) for k in
                   ("advances", "declines", "pct_above_20", "pct_above_50",
                    "pct_above_200", "new_highs", "new_lows", "n_symbols")})

    return {
        "universe":          universe_key,
        "name":              uni["name"],
        "description":       uni.get("description", ""),
        "dates":             list(window),
        "index":             {"symbol": uni["index"]["symbol"], "close": _clean(ic)},
        "series":            series,
        "concentration":     {"label": conc_label,
                              "ratio": _clean(conc.loc[window]) if conc is not None else None},
        "divergences":       [e for e in events if e["date"] in in_win],
        "active_divergences": active,
        "regime":            reg,
        "interpretation":    interp,
        "zbt_events":        [z for z in zbt if z in in_win],
        "status":            status,
        "note":              SURVIVORSHIP_NOTE,
        "updated":           datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_summary(universe_key=DEFAULT_UNIVERSE):
    """Compact daily readout — shared by /api/breadth/summary, the homepage
    badge, and the CLI printout."""
    cfg = universes.load_config()
    uni = cfg["universes"][universe_key]
    agg, der, index_close = _full_series(universe_key)
    if agg is None:
        return {"universe": universe_key, "name": uni["name"],
                "regime": None, "note": "no local data — run a backfill"}
    _, active, reg, zbt, interp = _analyze(agg, der, index_close)

    def _f(v):
        return None if pd.isna(v) else round(float(v), 2)

    return {
        "universe":       universe_key,
        "name":           uni["name"],
        "as_of":          agg.index[-1],
        "regime":         reg["state"],
        "score":          reg["score"],
        "reasons":        reg["reasons"],
        "interpretation": interp,
        "active_divergences": active,
        "metrics": {
            "mcclellan":     _f(der["mcclellan"].iloc[-1]),
            "summation":     _f(der["summation"].iloc[-1]),
            "trin":          _f(der["trin"].iloc[-1]),
            "pct_above_20":  _f(agg["pct_above_20"].iloc[-1]),
            "pct_above_50":  _f(agg["pct_above_50"].iloc[-1]),
            "pct_above_200": _f(agg["pct_above_200"].iloc[-1]),
            "net_advances":  _f(agg["advances"].iloc[-1] - agg["declines"].iloc[-1]),
            "nh_nl":         _f(der["nh_nl"].iloc[-1]),
        },
        "note": SURVIVORSHIP_NOTE,
    }


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_page(req):
    with open(_MODULE_DIR / "breadth.html") as f:
        return Response.html(f.read())


def _handle_universes(req):
    cfg = universes.load_config()
    out = []
    for key, uni in cfg["universes"].items():
        out.append({
            "key":         key,
            "name":        uni["name"],
            "description": uni.get("description", ""),
            "index":       uni["index"]["symbol"],
            "status":      store.universe_status(key),
        })
    return Response.json({
        "universes":  out,
        "datasource": cfg["settings"].get("datasource", "schwab"),
        "default":    DEFAULT_UNIVERSE,
    })


def _universe_or_error(req):
    key = req.qs.get("universe", [DEFAULT_UNIVERSE])[0]
    cfg = universes.load_config()
    if key not in cfg["universes"]:
        return None, Response.error(f"unknown universe '{key}'", 400)
    return key, None


def _handle_dashboard(req):
    key, err = _universe_or_error(req)
    if err:
        return err
    days = int(req.qs.get("days", [str(DEFAULT_DAYS)])[0])
    days = max(120, min(days, 5000))
    return Response.json(build_dashboard(key, days))


def _handle_summary(req):
    key, err = _universe_or_error(req)
    if err:
        return err
    return Response.json(build_summary(key))


def _handle_sync(req):
    body = req.json_body()
    key  = body.get("universe", DEFAULT_UNIVERSE)
    cfg  = universes.load_config()
    if key not in cfg["universes"]:
        return Response.error(f"unknown universe '{key}'", 400)
    ok, msg = backfill.start_sync(key, body.get("source"))
    return Response.json({"ok": ok, "message": msg}, status=200 if ok else 409)


def _handle_progress(req):
    return Response.json(backfill.get_progress())


def register_routes(router):
    store.init_db()
    router.get("/breadth.html",          _handle_page)
    router.get("/api/breadth/universes", _handle_universes)
    router.get("/api/breadth/dashboard", _handle_dashboard)
    router.get("/api/breadth/summary",   _handle_summary)
    router.get("/api/breadth/progress",  _handle_progress)
    router.post("/api/breadth/sync",     _handle_sync)
