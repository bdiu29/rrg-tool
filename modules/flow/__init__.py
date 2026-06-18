"""
Options-flow module — unusual options activity (whale entries/exits).

Watches the options chain for the small subset of flow worth acting on, encoding a
flow trader's 6-rule filter (`scoring`). Built on Schwab chain SNAPSHOTS via a
pluggable `source` adapter (a future Polygon trade-tape source flips the estimated
A/AA + sweep/block signals to confirmed with no scoring change). The intraday
`poller` sweeps the universe, the `store` persists flagged signals, and Rule-6
`context` annotates each with the harness's RRG/regime/golden-pocket/volume-profile
confluence.

Routes:
  GET  /flow.html
  GET  /api/flow/feed     → flagged signals (filterable)
  GET  /api/flow/contract → one contract's factor breakdown
  GET  /api/flow/summary  → hub badge (top conviction name + counts)
  GET  /api/flow/status   → poller + data-tier status + settings
  POST /api/flow/sync     → run one scan pass now
  POST /api/flow/settings → universe / channels / thresholds / source / interval
  POST /api/flow/poller   → start|stop the daemon
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from modules import Response
from modules.flow import notify, poller, store

_MODULE_DIR = Path(__file__).resolve().parent
_ET = ZoneInfo("America/New_York")

# Settings the user may override via POST /api/flow/settings (others are scoring
# constants that stay code-fixed by design).
_SETTABLE = ("universe", "flow_channels", "source", "interval", "burst_notional")


def _today():
    return datetime.now(_ET).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_page(req):
    with open(_MODULE_DIR / "flow.html") as f:
        return Response.html(f.read())


def _qs(req, key, default=None):
    return req.qs.get(key, [default])[0]


def _handle_feed(req):
    date = _qs(req, "date") or store.latest_signal_date(_today())
    try:
        min_conv = float(_qs(req, "min_conviction", "0") or 0)
    except ValueError:
        min_conv = 0
    conviction_only = _qs(req, "conviction_only", "1") in ("1", "true", "yes")
    # When drilling into specific tickers, show all flow on them (don't hide
    # watch/notable activity behind the default conviction-only cut).
    tickers = store._ticker_list(_qs(req, "tickers") or _qs(req, "underlying"))
    classification = None if tickers else ("conviction" if conviction_only else (_qs(req, "classification") or None))
    signals = store.list_flow_signals(
        date, min_conviction=min_conv, classification=classification,
        side=_qs(req, "side"), bucket=_qs(req, "bucket"),
        underlying=tickers,
        limit=int(_qs(req, "limit", "300") or 300))
    return Response.json({
        "date": date, "count": len(signals), "signals": signals,
        "tickers": tickers, "counts": store.classification_counts(date),
        "status": poller.status(),
    })


def _handle_contract(req):
    osym = _qs(req, "option_symbol")
    date = _qs(req, "date") or store.latest_signal_date(_today())
    if not osym:
        return Response.error("option_symbol required", 400)
    sig = store.get_flow_signal(date, osym)
    if not sig:
        return Response.error("no signal for that contract/date", 404)
    return Response.json(sig)


def _handle_summary(req):
    date = store.latest_signal_date(_today())
    top = store.list_flow_signals(date, classification="conviction", limit=1)
    notable = store.list_flow_signals(date, min_conviction=0, limit=300)
    n_conviction = sum(1 for s in notable if s["classification"] == "conviction")
    lead = None
    if top:
        t = top[0]
        lead = {"underlying": t["underlying"], "direction": t["direction"],
                "conviction": t["conviction"]}
    return Response.json({"date": date, "conviction_count": n_conviction,
                          "flagged_count": len(notable), "lead": lead})


def _handle_status(req):
    return Response.json({
        "poller":   poller.status(),
        "channels": notify.configured(),
        "settings": store.all_settings(),
    })


def _handle_sync(req):
    try:
        n = poller.run_pass()
        return Response.json({"ok": True, "flagged": n, "status": poller.status()})
    except Exception as e:
        return Response.error(f"sync failed: {e}", 500)


def _handle_settings(req):
    try:
        body = req.json_body() or {}
    except Exception:
        body = {}
    for k, v in body.items():
        if k in _SETTABLE:
            store.set_setting(k, v)
    return Response.json({"ok": True, "settings": store.all_settings()})


def _handle_poller(req):
    try:
        body = req.json_body() or {}
    except Exception:
        body = {}
    action = body.get("action", "start")
    if action == "stop":
        poller.stop()
    else:
        poller.start()
    return Response.json({"ok": True, "status": poller.status()})


def register_routes(router):
    router.get("/flow.html",          _handle_page)
    router.get("/api/flow/feed",      _handle_feed)
    router.get("/api/flow/contract",  _handle_contract)
    router.get("/api/flow/summary",   _handle_summary)
    router.get("/api/flow/status",    _handle_status)
    router.post("/api/flow/sync",     _handle_sync)
    router.post("/api/flow/settings", _handle_settings)
    router.post("/api/flow/poller",   _handle_poller)
    poller.start()                    # idempotent; idles outside market hours
