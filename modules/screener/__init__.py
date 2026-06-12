"""
Screener module — TradingView-style screener, watchlists, pump/dump alerts.

Scans run over a daily snapshot (one indicator row per symbol, rebuilt in the
background from breadth's bars) so every request is a sub-second pandas
filter. An intraday poller live-patches focus-list rows (positions ∪
watchlists) from Schwab quotes and fires alert rules; watchlists can route
their alerts to Discord/email.

Routes registered:
  GET  /screener.html                 → page
  GET  /api/screener/status           → snapshot/fundamentals/poller/alerts state
  POST /api/screener/refresh          → start background refresh {kind?}
  GET  /api/screener/progress         → refresh job status
  GET  /api/screener/fields           → filter field registry + ops
  POST /api/screener/scan             → run a screen {conditions|screen_id, universe, ...}
  POST /api/screener/backtest         → backtest a confluence {conditions|screen_id, universe, start, end, exit}
  GET  /api/screener/screens          → saved screens
  POST /api/screener/screens/save     → create/update {id?, name, universe, conditions, armed?}
  POST /api/screener/screens/delete   → {id}
  POST /api/screener/screens/arm      → {id, armed}
  GET  /api/screener/watchlists       → watchlists with symbols + channels
  POST /api/screener/watchlists/save  → {id?, name, symbols, channels}
  POST /api/screener/watchlists/delete→ {id}
  GET  /api/screener/alerts           → recent alerts (?days=&unacked=)
  POST /api/screener/alerts/ack       → {ids?|all?}
  GET  /api/screener/alerts/summary   → {today, unacked, by_symbol}
  POST /api/screener/notify/test      → {channel}
  POST /api/screener/settings         → {positions_channels}
  POST /api/screener/poller           → {action: start|stop}
"""

import math
from pathlib import Path

from modules import Response
from modules.breadth import store as breadth_store
from modules.breadth import universes as breadth_universes

from . import backtest, filters, notify, poller, snapshot, store

_MODULE_DIR = Path(__file__).resolve().parent

SURVIVORSHIP_NOTE = (
    "Universe membership is today's constituent list — names delisted after "
    "big declines are missing, so discovery scans skew toward survivors."
)

DEFAULT_LIMIT = 200
MAX_LIMIT     = 1000
DEFAULT_SORT  = "chg_pct"

# Columns returned per scan row (subset of the frame, JSON-cleaned)
SCAN_COLUMNS = [
    "date", "close", "chg_pct", "gap_pct", "volume", "rvol_10d", "vol_chg_pct",
    "rsi14", "atr14", "atr_pct", "rs_1m_pct", "rs_3m_pct",
    "pct_from_52w_high", "pct_from_52w_low",
    "price_vs_sma20_pct", "price_vs_sma50_pct",
    "price_vs_sma150_pct", "price_vs_sma200_pct",
    "price_vs_ema10_pct", "price_vs_ema20_pct", "price_vs_ema50_pct",
    "gp_direction", "gp_retrace", "gp_in_pocket", "gp_approaching",
    "gp_zone_low", "gp_zone_high",
    "market_cap", "pe_ratio", "div_yield", "beta",
    "days_to_earnings", "earnings_date", "sector", "sector_etf", "rrg_call",
]

CHANNELS = ("discord", "email")


def _safe(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (int, str, bool)):
        return v
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else round(f, 4)
    except (TypeError, ValueError):
        return str(v)


def _universe_keys():
    return set(breadth_universes.load_config()["universes"])


# ---------------------------------------------------------------------------
# Status / refresh
# ---------------------------------------------------------------------------

def _handle_page(req):
    with open(_MODULE_DIR / "screener.html") as f:
        return Response.html(f.read())


def _handle_status(req):
    info = store.snapshot_info()
    return Response.json({
        "snapshot_date":      store.get_meta("snapshot_date"),
        "bars_date":          snapshot.bars_date(),
        "n_symbols":          info["n_symbols"],
        "stale":              snapshot.needs_refresh(),
        "fundamentals":       store.fundamentals_coverage(),
        "poller":             poller.status(),
        "notify":             {"configured": notify.configured(),
                               "last_errors": notify.last_errors()},
        "alerts":             store.alerts_summary(),
        "positions_channels": sorted(store.get_positions_channels()),
        "note":               SURVIVORSHIP_NOTE,
    })


def _handle_refresh(req):
    kind = (req.json_body() or {}).get("kind", "snapshot")
    ok, msg = snapshot.start_refresh(kind)
    return Response.json({"ok": ok, "message": msg}, status=200 if ok else 409)


def _handle_progress(req):
    return Response.json(snapshot.get_progress())


def _handle_fields(req):
    return Response.json({
        "fields":  filters.FIELDS,
        "num_ops": list(filters.NUM_OPS),
        "str_ops": list(filters.STR_OPS),
    })


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _handle_scan(req):
    body = req.json_body() or {}

    if body.get("screen_id") is not None:
        screen = store.get_screen(body["screen_id"])
        if screen is None:
            return Response.error("unknown screen id", 404)
        conditions = screen["conditions"]
        universe   = body.get("universe") or screen["universe"]
    else:
        conditions = body.get("conditions", [])
        universe   = body.get("universe", "all")

    errors = filters.validate_conditions(conditions)
    if errors:
        return Response.error("; ".join(errors), 400)
    if universe != "all" and universe not in _universe_keys():
        return Response.error(f"unknown universe '{universe}'", 400)

    # keep the snapshot current without blocking the request
    if snapshot.needs_refresh():
        snapshot.start_refresh("snapshot")

    df, patched = poller.build_scan_frame(live=True)
    if df.empty:
        return Response.json({"rows": [], "total_matches": 0, "empty": True,
                              "as_of": None, "note": SURVIVORSHIP_NOTE})

    if universe != "all":
        members = set(breadth_store.get_members(universe))
        df = df[df.index.isin(members)]
    if body.get("symbols"):   # explicit list (e.g. a watchlist view)
        wanted = {str(s).upper() for s in body["symbols"]}
        df = df[df.index.isin(wanted)]

    matched = filters.apply_filters(df, conditions)
    total   = len(matched)

    sort = body.get("sort", DEFAULT_SORT)
    if sort not in matched.columns:
        sort = DEFAULT_SORT
    ascending = body.get("dir", "desc") == "asc"
    matched   = matched.sort_values(sort, ascending=ascending, na_position="last")

    limit   = max(1, min(int(body.get("limit", DEFAULT_LIMIT)), MAX_LIMIT))
    live    = set(patched)
    rows    = []
    for sym, row in matched.head(limit).iterrows():
        out = {"symbol": sym, "live": sym in live}
        for col in SCAN_COLUMNS:
            out[col] = _safe(row.get(col))
        rows.append(out)

    return Response.json({
        "rows":          rows,
        "total_matches": total,
        "as_of":         store.get_meta("snapshot_date"),
        "live_patched":  sorted(live),
        "stale":         snapshot.needs_refresh(),
        "note":          SURVIVORSHIP_NOTE,
    })


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def _handle_backtest(req):
    body = req.json_body() or {}

    if body.get("screen_id") is not None:
        screen = store.get_screen(body["screen_id"])
        if screen is None:
            return Response.error("unknown screen id", 404)
        conditions = screen["conditions"]
        universe   = body.get("universe") or screen["universe"]
    else:
        conditions = body.get("conditions", [])
        universe   = body.get("universe", backtest.DEFAULT_UNIVERSE)

    if not conditions:
        return Response.error("a backtest needs at least one condition", 400)
    errors = filters.validate_conditions(conditions)
    if errors:
        return Response.error("; ".join(errors), 400)
    if universe == "all":
        pass
    elif universe not in _universe_keys():
        return Response.error(f"unknown universe '{universe}'", 400)

    try:
        report = backtest.run_backtest(
            conditions, universe=universe,
            start=body.get("start"), end=body.get("end"),
            exit_cfg=body.get("exit"))
    except Exception as e:
        return Response.error(f"backtest failed: {e}", 500)
    if report.get("error"):
        return Response.error(report["error"], 400)
    return Response.json(report)


# ---------------------------------------------------------------------------
# Screens CRUD
# ---------------------------------------------------------------------------

def _handle_screens(req):
    return Response.json({"screens": store.list_screens()})


def _handle_screen_save(req):
    body = req.json_body() or {}
    name = (body.get("name") or "").strip()
    if not name:
        return Response.error("screen name required", 400)
    conditions = body.get("conditions", [])
    errors = filters.validate_conditions(conditions)
    if errors:
        return Response.error("; ".join(errors), 400)
    universe = body.get("universe", "all")
    if universe != "all" and universe not in _universe_keys():
        return Response.error(f"unknown universe '{universe}'", 400)
    try:
        sid = store.save_screen(name, universe, conditions,
                                screen_id=body.get("id"),
                                armed=body.get("armed"))
    except Exception as e:
        return Response.error(f"could not save screen: {e}", 400)
    return Response.json({"ok": True, "id": sid})


def _handle_screen_delete(req):
    body   = req.json_body() or {}
    screen = store.get_screen(body.get("id"))
    if screen is None:
        return Response.error("unknown screen id", 404)
    if screen["builtin"]:
        return Response.error("built-in screens can't be deleted", 400)
    store.delete_screen(screen["id"])
    return Response.json({"ok": True})


def _handle_screen_arm(req):
    body   = req.json_body() or {}
    screen = store.get_screen(body.get("id"))
    if screen is None:
        return Response.error("unknown screen id", 404)
    store.set_screen_armed(screen["id"], body.get("armed", True))
    return Response.json({"ok": True})


# ---------------------------------------------------------------------------
# Watchlists
# ---------------------------------------------------------------------------

def _handle_watchlists(req):
    return Response.json({"watchlists": store.list_watchlists()})


def _handle_watchlist_save(req):
    body = req.json_body() or {}
    name = (body.get("name") or "").strip()
    if not name:
        return Response.error("watchlist name required", 400)
    channels = [c for c in body.get("channels", []) if c in CHANNELS]
    try:
        wid = store.save_watchlist(name, body.get("symbols", []),
                                   channels=channels,
                                   watchlist_id=body.get("id"))
    except Exception as e:
        return Response.error(f"could not save watchlist: {e}", 400)
    return Response.json({"ok": True, "id": wid})


def _handle_watchlist_delete(req):
    body = req.json_body() or {}
    store.delete_watchlist(body.get("id"))
    return Response.json({"ok": True})


# ---------------------------------------------------------------------------
# Alerts / notify / settings / poller
# ---------------------------------------------------------------------------

def _handle_alerts(req):
    days    = int(req.qs.get("days", ["5"])[0])
    unacked = req.qs.get("unacked", ["0"])[0] in ("1", "true")
    return Response.json({"alerts": store.list_alerts(days=days,
                                                      unacked_only=unacked)})


def _handle_alerts_ack(req):
    body = req.json_body() or {}
    store.ack_alerts(ids=body.get("ids"), all_alerts=bool(body.get("all")))
    return Response.json({"ok": True})


def _handle_alerts_summary(req):
    return Response.json(store.alerts_summary())


def _handle_notify_test(req):
    channel  = (req.json_body() or {}).get("channel")
    ok, err  = notify.send_test(channel)
    return Response.json({"ok": ok, "error": err}, status=200 if ok else 502)


def _handle_settings(req):
    body = req.json_body() or {}
    if "positions_channels" in body:
        store.set_positions_channels(
            [c for c in body["positions_channels"] if c in CHANNELS])
    return Response.json({"ok": True,
                          "positions_channels": sorted(store.get_positions_channels())})


def _handle_poller(req):
    action = (req.json_body() or {}).get("action")
    if action == "start":
        poller.start()
    elif action == "stop":
        poller.stop()
    else:
        return Response.error("action must be 'start' or 'stop'", 400)
    return Response.json({"ok": True, "poller": poller.status()})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_routes(router):
    store.init_db()
    store.seed_builtin_screens()
    poller.start()

    router.get("/screener.html",                 _handle_page)
    router.get("/api/screener/status",           _handle_status)
    router.post("/api/screener/refresh",         _handle_refresh)
    router.get("/api/screener/progress",         _handle_progress)
    router.get("/api/screener/fields",           _handle_fields)
    router.post("/api/screener/scan",            _handle_scan)
    router.post("/api/screener/backtest",        _handle_backtest)
    router.get("/api/screener/screens",          _handle_screens)
    router.post("/api/screener/screens/save",    _handle_screen_save)
    router.post("/api/screener/screens/delete",  _handle_screen_delete)
    router.post("/api/screener/screens/arm",     _handle_screen_arm)
    router.get("/api/screener/watchlists",       _handle_watchlists)
    router.post("/api/screener/watchlists/save", _handle_watchlist_save)
    router.post("/api/screener/watchlists/delete", _handle_watchlist_delete)
    router.get("/api/screener/alerts",           _handle_alerts)
    router.post("/api/screener/alerts/ack",      _handle_alerts_ack)
    router.get("/api/screener/alerts/summary",   _handle_alerts_summary)
    router.post("/api/screener/notify/test",     _handle_notify_test)
    router.post("/api/screener/settings",        _handle_settings)
    router.post("/api/screener/poller",          _handle_poller)
