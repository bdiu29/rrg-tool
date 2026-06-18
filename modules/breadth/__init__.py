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
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from modules import Response

from . import backfill, datasource, indicators, regime, store, universes

_MODULE_DIR = Path(__file__).resolve().parent

SURVIVORSHIP_NOTE = (
    "Universe membership is today's constituent list — deep historical "
    "breadth is survivorship-biased (delisted losers missing). Recent "
    "readings are reliable; treat multi-year history as approximate."
)

DEFAULT_UNIVERSE = "sp500"
DEFAULT_DAYS     = 504    # ~2 trading years displayed by default

# Breadth-tape scope. "all" = the broad NYSE+Nasdaq tape so the ±4%/±25%
# counts land in the hundreds (sp500 alone is single-digit). The single-
# universe keys mirror the dashboard's universes.
TAPE_UNIVERSES = {
    "all":    "All US (NYSE + Nasdaq)",
    "sp500":  "S&P 500",
    "nyse":   "NYSE",
    "nasdaq": "Nasdaq",
}
DEFAULT_TAPE_UNIVERSE = "all"
DEFAULT_TAPE_ROWS     = 30
SP_INDEX_SYMBOL       = "^GSPC"   # real S&P 500 level for the tape's S&P column

_TAPE_CACHE = {}   # (universe, rows) → (last_date, payload); invalidated by a new sync


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
                    "pct_above_200", "pct_above_5ema", "pct_above_10ema",
                    "pct_above_20ema", "n_above_5ema", "n_above_10ema",
                    "n_above_20ema", "new_highs", "new_lows", "n_symbols")})

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
            "advances":      _f(agg["advances"].iloc[-1]),
            "declines":      _f(agg["declines"].iloc[-1]),
            "nh_nl":         _f(der["nh_nl"].iloc[-1]),
        },
        "note": SURVIVORSHIP_NOTE,
    }


# ---------------------------------------------------------------------------
# Breadth tape (Stockbee-style Market Monitor) — the breadth page's second tab
# ---------------------------------------------------------------------------

def _tape_members(universe_key):
    """(display name, member list) for a tape universe. 'all' = NYSE ∪ Nasdaq."""
    if universe_key == "all":
        members = sorted(set(store.get_members("nyse")) |
                         set(store.get_members("nasdaq")))
    else:
        members = store.get_members(universe_key)
    return TAPE_UNIVERSES.get(universe_key, universe_key), members


def _tape_status(universe_key):
    """Coverage summary; for 'all' it merges the two underlying universes."""
    if universe_key != "all":
        return store.universe_status(universe_key)
    n = len(set(store.get_members("nyse")) | set(store.get_members("nasdaq")))
    s = store.universe_status("nasdaq")
    return {"members": n, "last_date": s.get("last_date"),
            "breadth_days": s.get("breadth_days")}


def _ensure_gspc(target_date):
    """Lazily cache ^GSPC bars (the tape's real-S&P column). yfinance only —
    one symbol, idempotent upsert, fail-soft (column shows null on failure)."""
    try:
        last = store.last_bar_date(SP_INDEX_SYMBOL)
        if last and str(last) >= str(target_date):
            return
        start = (datetime.now() - timedelta(days=365 * 3 + 10)).strftime("%Y-%m-%d")
        end   = datetime.now().strftime("%Y-%m-%d")
        df = datasource.YFinanceDataSource().get_price_history(SP_INDEX_SYMBOL, start, end)
        if df is not None and not df.empty:
            store.upsert_bars(SP_INDEX_SYMBOL, df)
    except Exception:
        pass


# Output columns in display order (date + S&P handled separately).
_TAPE_COLS = ["up4", "down4", "ratio5", "ratio10", "up25q", "down25q",
              "up25m", "down25m", "up50m", "down50m", "up13_34", "down13_34",
              "atr_ext", "pct_above_50", "n_symbols", "sp"]


def build_tape(universe_key=DEFAULT_TAPE_UNIVERSE, rows=DEFAULT_TAPE_ROWS):
    rows = max(5, min(int(rows), 250))
    if universe_key not in TAPE_UNIVERSES:
        universe_key = DEFAULT_TAPE_UNIVERSE
    name, members = _tape_members(universe_key)
    status = _tape_status(universe_key)

    empty = {"universe": universe_key, "name": name, "empty": True,
             "status": status, "note": SURVIVORSHIP_NOTE}
    if not members:
        return empty

    # Cache on the universe's newest stored date — a sync advances it and busts
    # the entry, so repeat loads skip the multi-second panel recompute.
    ckey = (universe_key, rows)
    cached = _TAPE_CACHE.get(ckey)
    if cached and cached[0] == status.get("last_date"):
        return cached[1]

    close, high, low = store.get_panels(members, fields=("close", "high", "low"))
    if close.empty:
        return empty
    mm = indicators.market_monitor(close, high, low)
    if mm.empty:
        return empty

    _ensure_gspc(mm.index[-1])
    sp = store.get_series(SP_INDEX_SYMBOL)
    mm["sp"] = (sp["close"].reindex(mm.index) if not sp.empty
                else pd.Series(np.nan, index=mm.index))

    tail    = mm.iloc[-rows:]
    dates   = list(tail.index)
    cleaned = {c: _clean(tail[c]) for c in _TAPE_COLS}
    out_rows = []
    for i in range(len(dates) - 1, -1, -1):                 # newest day first
        row = {"date": dates[i]}
        row.update({c: cleaned[c][i] for c in _TAPE_COLS})
        out_rows.append(row)

    last = mm.iloc[-1]
    adv, dec = float(last["advances"]), float(last["declines"])
    nh, nl   = float(last["new_highs"]), float(last["new_lows"])

    def _pct(a, b):
        return round(100.0 * a / (a + b), 1) if (a + b) > 0 else None

    payload = {
        "universe": universe_key,
        "name":     name,
        "as_of":    dates[-1],
        "rows":     out_rows,
        "gauges": {
            "advances": int(adv), "declines": int(dec),
            "adv_pct": _pct(adv, dec), "dec_pct": _pct(dec, adv),
            "new_highs": int(nh), "new_lows": int(nl),
            "nh_pct": _pct(nh, nl), "nl_pct": _pct(nl, nh),
        },
        "status":  status,
        "note":    SURVIVORSHIP_NOTE,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _TAPE_CACHE[ckey] = (status.get("last_date"), payload)
    return payload


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


def _handle_tape(req):
    key  = req.qs.get("universe", [DEFAULT_TAPE_UNIVERSE])[0]
    rows = req.qs.get("rows", [str(DEFAULT_TAPE_ROWS)])[0]
    try:
        rows = int(rows)
    except (TypeError, ValueError):
        rows = DEFAULT_TAPE_ROWS
    return Response.json(build_tape(key, rows))


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
    router.get("/api/breadth/tape",      _handle_tape)
    router.get("/api/breadth/progress",  _handle_progress)
    router.post("/api/breadth/sync",     _handle_sync)
