"""
Harness module — the AI subagent harness (CLAUDE.md Planned Expansion #3, the
project end-goal).

The absolute TOP consumer: it turns every data module's signal into a signed VOTE
(`votes.py`), combines them deterministically + arbitrates by regime (`combiner.py`),
and has Claude NARRATE the result into a daily brief on the subscription (`agents.py`).
The decision is the LLM-free combiner; the LLM only explains it, so the brief still
renders at $0 / offline. Nothing imports this module back.

Routes:
  GET  /harness.html        → the brief page
  GET  /api/harness         → full brief payload (cached; generates once/day)
  GET  /api/harness/summary → hub badge (stance + score + top long; never triggers an LLM call)
  POST /api/harness/run     → force regenerate (re-runs the LLM)

Cost posture: on-demand + TTL/file cache (the news `ensure_fresh` pattern, no
daemon). By default ONE master synthesis call per refresh; per-domain rationales
default to free templates (set HARNESS_DOMAIN_LLM=1 to narrate each with Haiku).
"""

import json
import os
import time
from datetime import date, datetime
from pathlib import Path

from modules import Response
from modules.harness import votes as votes_mod
from modules.harness import combiner
from modules.harness import agents

_MODULE_DIR = Path(__file__).resolve().parent
_DATA_DIR   = _MODULE_DIR / "data"
_TTL        = 30 * 60          # in-memory freshness, mirrors news ensure_fresh

_MEM = {"date": None, "at": 0.0, "payload": None}

_CAVEATS = [
    "The decision is a DETERMINISTIC, LLM-free combiner (replayable / backtestable); "
    "Claude only narrates it.",
    "Each module is a VOTE — no single signal has standalone forward alpha in a "
    "concentration regime; the edge is confluence + regime arbitration.",
    "Stance: CONCENTRATE when breadth is narrow / rotation off, ROTATE when broad — "
    "detect-and-adapt, not 'beat the regime'.",
    "Votes are only as good as the underlying data — run a breadth backfill + screener "
    "snapshot for the full set (missing modules fail soft to abstain).",
    "LLM narration runs on your Claude subscription via the local `claude` CLI (no API "
    "key); if it's unavailable the brief falls back to a deterministic template.",
]


# ---------------------------------------------------------------------------
# Brief assembly + cache
# ---------------------------------------------------------------------------

def _file(d):
    return _DATA_DIR / f"brief_{d}.json"


def _load_file(d):
    f = _file(d)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return None
    return None


def _save(payload):
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _file(payload["date"]).write_text(json.dumps(payload))
    except Exception:
        pass
    _MEM.update(date=payload["date"], at=time.time(), payload=payload)


def build_brief(llm=True):
    """Gather votes → combine → narrate → cache. Always recomputes (the caller
    decides whether to use the cache first via `get_brief`)."""
    today = date.today().isoformat()
    votes, regime, rotation = votes_mod.gather_all()
    combined = combiner.combine(votes, regime, rotation)

    domain_llm = llm and os.environ.get("HARNESS_DOMAIN_LLM", "0") == "1"
    for v in votes:
        v["rationale"] = (agents.domain_rationale(v) if domain_llm
                          else agents._template_rationale(v) if v.get("ok")
                          else (v.get("note") or "no data"))

    if llm:
        brief, llm_used = agents.master_brief(votes, combined, regime, rotation)
    else:
        brief, llm_used = agents._template_brief(votes, combined), False

    payload = {
        "date":          today,
        "generated_at":  datetime.now().isoformat(timespec="seconds"),
        "combined":      combined,
        "votes":         votes,
        "brief":         brief,
        "llm_used":      llm_used,
        "llm_available": agents.available(),
        "caveats":       _CAVEATS,
    }
    _save(payload)
    return payload


def get_brief(force=False):
    """GET path: serve the cached brief (memory → today's file), else generate once
    (with LLM narration if available). `force` always regenerates."""
    today = date.today().isoformat()
    if not force:
        if _MEM["date"] == today and time.time() - _MEM["at"] < _TTL and _MEM["payload"]:
            return _MEM["payload"]
        cached = _load_file(today)
        if cached:
            _MEM.update(date=today, at=time.time(), payload=cached)
            return cached
    return build_brief(llm=agents.available())


def _summary(payload):
    c = payload["combined"]
    top = c["longs"][0]["ticker"] if c.get("longs") else None
    text = f"{c['stance']} · {c['score']:+.0f}" + (f" · {top}" if top else "")
    status = {"ROTATE": "ok", "CONCENTRATE": "accent", "NEUTRAL": "neutral"}.get(
        c["stance"], "neutral")
    return {"text": text, "status": status, "stance": c["stance"],
            "score": c["score"], "posture": c["posture"], "top_long": top}


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_page(req):
    with open(_MODULE_DIR / "harness.html") as f:
        return Response.html(f.read())


def _handle_api(req):
    try:
        return Response.json(get_brief(force=False))
    except Exception as e:
        return Response.error(str(e))


def _handle_summary(req):
    """Hub badge — reads the cache only; never triggers an LLM call or a full
    recompute on the homepage."""
    today = date.today().isoformat()
    payload = (_MEM["payload"] if _MEM["date"] == today else None) or _load_file(today)
    if not payload:
        return Response.json({"text": "not generated", "status": "neutral"})
    return Response.json(_summary(payload))


def _handle_run(req):
    try:
        payload = build_brief(llm=True)
        return Response.json(payload)
    except Exception as e:
        return Response.error(str(e))


def _handle_watchlist(req):
    """GET → the stored focus watchlist; POST {text, replace} → import a TradingView
    CSV/watchlist export (parse + persist)."""
    from modules.harness import store, watchlist
    if req.method == "POST":
        try:
            body = req.json_body() or {}
        except Exception:
            body = {}
        text = body.get("text") or ""
        if not text.strip():
            return Response.error("no watchlist text uploaded", 400)
        res = watchlist.import_text(text, source="upload",
                                    replace=bool(body.get("replace", True)))
        return Response.json(res)
    return Response.json({"symbols": store.get_watchlist()})


def _handle_picks(req):
    """The ranked impulse×hold suggestions for the stored watchlist (on-demand)."""
    try:
        from modules.harness import picks
        return Response.json(picks.suggest())
    except Exception as e:
        return Response.error(f"picks failed: {e}", 500)


def _handle_paper(req):
    """GET → both paper books' state + the gate + daemon/mode. POST /step → advance one
    trading day (idempotent). POST /reset → wipe the books (requires {confirm:true})."""
    from modules.harness import paper, paper_poller, store
    path = req.path.rstrip("/")
    try:
        if path.endswith("/step"):
            return Response.json(paper.step())
        if path.endswith("/reset"):
            body = req.json_body() if req.method == "POST" else {}
            if not (body or {}).get("confirm"):
                return Response.error("reset requires {\"confirm\": true}", 400)
            store.reset_paper()
            return Response.json({"reset": True})
        st = paper.state()
        st["mode"] = store.get_setting("trading_mode", "manual")
        st["daemon"] = paper_poller.status()
        return Response.json(st)
    except Exception as e:
        return Response.error(f"paper failed: {e}", 500)


VALID_MODES = ("manual", "autonomous")


def _handle_mode(req):
    """GET → the trading mode + daemon status. POST {mode} → switch Manual ⇄ Autonomous
    (autonomous = the daemon auto-steps the PAPER book at the close; never live)."""
    from modules.harness import store, paper_poller
    if req.method == "POST":
        try:
            body = req.json_body() or {}
        except Exception:
            body = {}
        mode = str(body.get("mode", "")).lower()
        if mode not in VALID_MODES:
            return Response.error(f"mode must be one of {VALID_MODES}", 400)
        store.set_setting("trading_mode", mode)
        paper_poller.start()                       # idempotent; inert until autonomous
    return Response.json({"mode": store.get_setting("trading_mode", "manual"),
                          "daemon": paper_poller.status()})


def _handle_chat(req):
    """POST {message, history} → a grounded answer (anchored to the deterministic state)."""
    try:
        body = req.json_body() or {}
    except Exception:
        body = {}
    try:
        from modules.harness import chat
        return Response.json(chat.answer(body.get("message", ""), body.get("history")))
    except Exception as e:
        return Response.error(f"chat failed: {e}", 500)


def _handle_backtest(req):
    """The referee (Phase 2) — A/B the harness confluence decision vs raw RRG vs beta
    over history. Synchronous (sector daily ~4s); the LLM plays no part."""
    try:
        body = req.json_body() or {}
    except Exception:
        body = {}
    try:
        from modules.harness import backtest
        rep = backtest.run_harness_backtest(
            interval=body.get("interval", "1d"),
            universe=body.get("universe"),
            benchmark=body.get("benchmark"),
            portfolio=body.get("portfolio"),
        )
        return Response.json(rep)
    except Exception as e:
        return Response.error(f"backtest failed: {e}", 500)


def register_routes(router):
    try:
        from modules.harness import store, paper_poller
        store.init_db()
        paper_poller.start()        # idempotent; a no-op until the mode is autonomous
    except Exception:
        pass
    router.get("/harness.html",          _handle_page)
    router.get("/api/harness",           _handle_api)
    router.get("/api/harness/summary",   _handle_summary)
    router.post("/api/harness/run",      _handle_run)
    router.post("/api/harness/backtest", _handle_backtest)
    router.get("/api/harness/watchlist",  _handle_watchlist)
    router.post("/api/harness/watchlist", _handle_watchlist)
    router.get("/api/harness/picks",      _handle_picks)
    router.get("/api/harness/paper",        _handle_paper)
    router.post("/api/harness/paper/step",  _handle_paper)
    router.post("/api/harness/paper/reset", _handle_paper)
    router.get("/api/harness/mode",  _handle_mode)
    router.post("/api/harness/mode", _handle_mode)
    router.post("/api/harness/chat", _handle_chat)
