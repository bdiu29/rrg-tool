"""
News & Macro Events module — v1: economic calendar + Fed events.

A self-contained calendar of market-moving events (FOMC decisions/minutes, econ
data releases, Fed speeches, focus-list earnings), assembled from free sources
(no paid key required; FRED_API_KEY upgrades the econ calendar to exact dates).
Exposes an event-risk hook other pages overlay so the harness knows when to
"size smaller, tighten into the print."

Phase 2 (news feed: RSS + EDGAR 8-K + sentiment APIs) and Phase 3 (fed-funds
probabilities) slot in as new sources / tabs without touching this assembly.
"""

from pathlib import Path

from modules import Response
from . import calendar, sources, store

_HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_page(req):
    return Response.html((_HERE / "news.html").read_text())


def _qs_int(req, key, default):
    try:
        return int(req.qs.get(key, [default])[0])
    except (ValueError, TypeError):
        return default


def _handle_calendar(req):
    """Week-by-week calendar. ?track=econ|earnings & ?weeks=N & ?importance=high."""
    track = (req.qs.get("track", ["econ"])[0] or "econ").lower()
    if track not in ("econ", "earnings"):
        track = "econ"
    data = calendar.build_week_calendar(
        track=track,
        weeks=max(1, min(_qs_int(req, "weeks", 3), 8)),
        importance=req.qs.get("importance"),
    )
    return Response.json(data)


def _handle_feed(req):
    focus = (req.qs.get("focus", ["0"])[0] or "0").lower() in ("1", "true", "yes")
    data  = calendar.build_news_feed(
        focus_only=focus,
        limit=max(10, min(_qs_int(req, "limit", 80), 200)),
        days_back=max(1, min(_qs_int(req, "days", 5), 21)),
    )
    return Response.json(data)


def _handle_rates(req):
    return Response.json(calendar.build_rates())


def _handle_event_risk(req):
    return Response.json(calendar.event_risk())


def _handle_summary(req):
    return Response.json(calendar.summary())


def _handle_refresh(req):
    contributed = calendar.ensure_fresh(force=True)
    return Response.json({"ok": True, "contributed": contributed})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_routes(router):
    store.init_db()
    router.get("/news.html",            _handle_page)
    router.get("/api/news/calendar",    _handle_calendar)
    router.get("/api/news/feed",        _handle_feed)
    router.get("/api/news/rates",       _handle_rates)
    router.get("/api/news/event-risk",  _handle_event_risk)
    router.get("/api/news/summary",     _handle_summary)
    router.post("/api/news/refresh",    _handle_refresh)
