"""
Calendar assembly + the event-risk signal hook.

Merges every EventSource into the store (fail-soft, TTL-cached so back-to-back
requests don't re-hit the network), then serves:
  build_calendar()  — upcoming + recent events grouped for the page
  event_risk()      — the signal hook other pages overlay (next high-impact
                      macro event + a "smaller, tighter into the print" flag)
  summary()         — the hub badge

Slow-moving data, so there's no daemon: the first request after a TTL window
refreshes inline (the rankings-holdings pattern), and POST /api/news/refresh
forces it.
"""

import json
import threading
import time
from datetime import date, datetime, timedelta

from . import sources, store

TTL_SECONDS    = 1800        # 30 min — calendar data barely moves intraday
WINDOW_AHEAD   = 45          # days of forward calendar to fetch/keep
WINDOW_BACK    = 7           # days of recent items (Fed speeches, past prints)
HIGH_KINDS     = ("fomc", "econ")     # what counts as "macro event risk"
ECON_KINDS     = ("econ", "fomc")     # the economic-calendar track
EARNINGS_KINDS = ("earnings",)        # the earnings-calendar track
NEWS_KINDS     = ("news",)            # the Phase-2 headline feed
IMPORTANCE_RANK = {"high": 3, "med": 2, "low": 1}

_lock          = threading.Lock()
_last_refresh  = None        # time.monotonic() of the last successful refresh


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def refresh(days_ahead=WINDOW_AHEAD, days_back=WINDOW_BACK):
    """Fetch every source (fail-soft) and upsert into the store. Returns a
    {source_name: count} dict of what each contributed."""
    today = date.today()
    start = today - timedelta(days=days_back)
    end   = today + timedelta(days=days_ahead)
    contributed = {}
    for src in sources.ALL_SOURCES:
        try:
            events = src.fetch(start, end) or []
        except Exception:
            events = []
        store.upsert_events(events)
        contributed[src.name] = len(events)
    global _last_refresh
    _last_refresh = time.monotonic()
    return contributed


def ensure_fresh(force=False):
    """Refresh if the TTL has lapsed (thread-safe, single-flight)."""
    with _lock:
        if force or _last_refresh is None or (time.monotonic() - _last_refresh) > TTL_SECONDS:
            return refresh()
    return None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _days_until(event_date, today=None):
    today = today or date.today()
    return (datetime.strptime(event_date, "%Y-%m-%d").date() - today).days


def _decorate(ev, today):
    du = _days_until(ev["event_date"], today)
    out = dict(ev)
    out["days_until"] = du
    out["when"] = ("today" if du == 0 else "tomorrow" if du == 1
                   else f"in {du}d" if du > 0
                   else "yesterday" if du == -1 else f"{-du}d ago")
    return out


def build_calendar(days_ahead=WINDOW_AHEAD, days_back=WINDOW_BACK, importance=None):
    """→ {upcoming, recent, as_of, fred_keyed, note}. `importance` is an optional
    list filter (e.g. ['high','med'])."""
    ensure_fresh()
    today = date.today()
    start = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end   = (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    rows  = store.get_events(start, end, importance=importance)

    upcoming, recent = [], []
    for ev in rows:
        d = _decorate(ev, today)
        (upcoming if d["days_until"] >= 0 else recent).append(d)
    recent.reverse()    # most-recent first

    keyed = bool(sources.fred_key())
    return {
        "as_of":      today.strftime("%Y-%m-%d"),
        "upcoming":   upcoming,
        "recent":     recent,
        "fred_keyed": keyed,
        "note": ("Econ dates from FRED." if keyed else
                 "Econ dates are approximate — set FRED_API_KEY in .env for the "
                 "exact published release calendar."),
    }


# ---------------------------------------------------------------------------
# Week-by-week calendar (MarketWatch-style layout: day headers + columns)
# ---------------------------------------------------------------------------

def _fmt_time(t):
    """'08:30' → '8:30 am'; falsy → ''."""
    if not t:
        return ""
    try:
        hh, mm = t.split(":")
        h = int(hh)
        return f"{h % 12 or 12}:{mm} {'am' if h < 12 else 'pm'}"
    except Exception:
        return t


def _econ_period(ev):
    """Best-effort reference period for an econ release: monthly indicators report
    the prior month, GDP the prior quarter (a heuristic — exact for our curated set,
    which all lag ~1 month). FOMC has no data period."""
    if ev["kind"] != "econ":
        return ""
    title = (ev["title"] or "").lower()
    d     = datetime.strptime(ev["event_date"], "%Y-%m-%d").date()
    prev  = d.replace(day=1) - timedelta(days=1)
    if "gdp" in title or "gross domestic" in title:
        return f"Q{(prev.month - 1) // 3 + 1} {prev.year}"
    return prev.strftime("%b")


def _decorate_event(ev):
    extra = {}
    if ev.get("extra"):
        try:
            extra = json.loads(ev["extra"])
        except Exception:
            extra = {}
    actual = previous = ""
    if ev["kind"] == "econ":
        try:
            actual, previous = sources.econ_actual_previous(ev["title"], ev["event_date"])
        except Exception:
            pass
    return {
        "kind":       ev["kind"],
        "event_date": ev["event_date"],
        "time":       _fmt_time(ev.get("event_time")),
        "title":      ev["title"],
        "symbols":    ev.get("symbols"),
        "importance": ev.get("importance"),
        "url":        ev.get("url"),
        "detail":     ev.get("detail"),
        "period":     _econ_period(ev) if ev["kind"] == "econ" else (extra.get("period") or ""),
        "actual":     actual,
        "previous":   previous,
        "extra":      extra,
    }


def build_week_calendar(track="econ", weeks=3, importance=None):
    """→ {track, as_of, weeks:[{label, days:[{label, events, is_today, is_past}]}], note}.
    Weeks run Monday→Friday from the current week's Monday. Empty weekdays carry an
    empty event list (the frontend renders 'None scheduled')."""
    ensure_fresh()
    today  = date.today()
    kinds  = list(ECON_KINDS if track == "econ" else EARNINGS_KINDS)
    monday = today - timedelta(days=today.weekday())
    end    = monday + timedelta(days=weeks * 7 - 1)
    rows   = store.get_events(monday.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                              importance=importance, kinds=kinds)

    by_date = {}
    for ev in rows:
        by_date.setdefault(ev["event_date"], []).append(ev)

    weeks_out = []
    for w in range(weeks):
        wk_start, days = monday + timedelta(days=w * 7), []
        for dow in range(5):                       # Mon..Fri
            dd  = wk_start + timedelta(days=dow)
            ds  = dd.strftime("%Y-%m-%d")
            raw = sorted(by_date.get(ds, []), key=lambda e: e.get("event_time") or "99:99")
            days.append({
                "date":     ds,
                "label":    f"{dd.strftime('%A, %B')} {dd.day}".upper(),
                "is_today": dd == today,
                "is_past":  dd < today,
                "events":   [_decorate_event(ev) for ev in raw],
            })
        weeks_out.append({
            "week_start": wk_start.strftime("%Y-%m-%d"),
            "label":      f"WEEK OF {wk_start.strftime('%B')} {wk_start.day}".upper(),
            "days":       days,
        })

    if track == "earnings":
        note = ("Earnings + EPS estimates from Finnhub." if sources.finnhub_key() else
                "Earnings from your focus list — set FINNHUB_API_KEY in .env for full "
                "coverage (S&P 500) + EPS estimates.")
    elif sources.fred_key():
        note = ("Dates + Actual / Previous from FRED (latest revised values). "
                "Median Forecast (consensus) needs a paid feed.")
    else:
        note = ("Dates approximate & Actual / Previous blank — set FRED_API_KEY for the exact "
                "release calendar + FRED values. Consensus (Median Forecast) needs a paid feed.")

    return {
        "track": track, "as_of": today.strftime("%Y-%m-%d"), "weeks": weeks_out,
        "fred_keyed": bool(sources.fred_key()), "finnhub_keyed": bool(sources.finnhub_key()),
        "note": note,
    }


def build_news_feed(focus_only=False, limit=80, days_back=5):
    """Phase-2 headline feed → {as_of, days:[{label, items}], count, focus_only, note}.
    Reverse-chronological, grouped by day (the calendar's day aesthetic). `focus_only`
    keeps items tagged with a focus-list ticker (positions ∪ watchlists)."""
    ensure_fresh()
    today = date.today()
    rows  = store.get_events((today - timedelta(days=days_back)).strftime("%Y-%m-%d"),
                             today.strftime("%Y-%m-%d"), kinds=list(NEWS_KINDS))
    focus = set()
    if focus_only:
        try:
            from modules.screener.poller import focus_list
            focus = set(focus_list())
        except Exception:
            focus = set()

    items = []
    for ev in rows:
        x = {}
        if ev.get("extra"):
            try:
                x = json.loads(ev["extra"])
            except Exception:
                x = {}
        tickers = x.get("tickers") or [s for s in (ev.get("symbols") or "").split(",") if s]
        if focus_only and not (set(tickers) & focus):
            continue
        items.append({
            "date":     ev["event_date"],
            "time":     _fmt_time(ev.get("event_time")),
            "time_raw": ev.get("event_time") or "",
            "title":    ev["title"],
            "url":      ev.get("url"),
            "source":   x.get("source_name") or ev.get("source"),
            "tickers":  tickers,
            "sentiment": x.get("sentiment"),
        })
    items.sort(key=lambda i: (i["date"], i["time_raw"]), reverse=True)
    items = items[:limit]

    by_day = {}
    for it in items:
        by_day.setdefault(it["date"], []).append(it)
    days = []
    for ds in sorted(by_day, reverse=True):
        dd = datetime.strptime(ds, "%Y-%m-%d").date()
        days.append({"date": ds, "label": f"{dd.strftime('%A, %B')} {dd.day}".upper(),
                     "is_today": dd == today, "items": by_day[ds]})

    av_keyed   = bool(sources.alphavantage_key())
    poly_keyed = bool(sources.polygon_key())
    enrich = [n for n, on in (("Polygon", poly_keyed), ("AlphaVantage", av_keyed)) if on]
    note = "Market headlines (RSS) + SEC 8-K filings"
    if enrich:
        note += " + " + " & ".join(enrich) + " sentiment & ticker tagging."
    else:
        note += "; set POLYGON_IO_KEY or ALPHAVANTAGE_API_KEY for sentiment + ticker tagging."
    return {"as_of": today.strftime("%Y-%m-%d"), "days": days, "count": len(items),
            "focus_only": focus_only, "av_keyed": av_keyed, "poly_keyed": poly_keyed,
            "note": note}


# ---------------------------------------------------------------------------
# Macro tab — rates & yield curve from FRED (the regime-context read)
# ---------------------------------------------------------------------------

# (FRED series, label, kind). Spreads flag the curve inversion.
RATE_SERIES = [
    ("DFF",    "Fed Funds (effective)", "rate"),
    ("DGS3MO", "3-Month",               "rate"),
    ("DGS2",   "2-Year",                "rate"),
    ("DGS5",   "5-Year",                "rate"),
    ("DGS10",  "10-Year",               "rate"),
    ("DGS30",  "30-Year",               "rate"),
    ("DFII10", "10-Year real (TIPS)",   "rate"),
    ("T10Y2Y", "10Y – 2Y spread",       "spread"),
    ("T10Y3M", "10Y – 3M spread",       "spread"),
]
CURVE_TENORS = [("DGS3MO", "3M"), ("DGS2", "2Y"), ("DGS5", "5Y"),
                ("DGS10", "10Y"), ("DGS30", "30Y")]


def _bps(delta):
    """Percentage-point change → integer basis points."""
    return None if delta is None else round(delta * 100)


def build_rates():
    """→ {fred_keyed, as_of, rates:[{label,kind,value,d1,d5,d21,inverted}], curve,
    inversion, note}. Rates need FRED (free key) — there's no clean keyless source for
    the 2Y / fed funds / curve spreads, so without a key it returns an empty CTA state."""
    if not sources.fred_key():
        return {"fred_keyed": False, "as_of": None, "rates": [], "curve": [],
                "inversion": None,
                "note": "Set FRED_API_KEY in .env (free) for the rates & yield-curve view."}

    seqs, rows, as_of = {}, [], None
    for series, label, kind in RATE_SERIES:
        obs = sources.fred_observations(series, "lin", limit=45)
        seq = sorted(obs.items(), key=lambda kv: kv[0], reverse=True)   # newest first
        seqs[series] = seq
        if not seq:
            rows.append({"series": series, "label": label, "kind": kind, "value": None,
                         "d1": None, "d5": None, "d21": None, "inverted": None})
            continue
        d0, v0 = seq[0]
        as_of  = max(as_of, d0) if as_of else d0
        back   = lambda n: seq[n][1] if len(seq) > n else None
        rows.append({
            "series": series, "label": label, "kind": kind, "value": round(v0, 2),
            "d1":  _bps(v0 - back(1))  if back(1)  is not None else None,
            "d5":  _bps(v0 - back(5))  if back(5)  is not None else None,
            "d21": _bps(v0 - back(21)) if back(21) is not None else None,
            "inverted": (v0 < 0) if kind == "spread" else None,
        })

    curve = [{"tenor": t, "value": round(seqs[s][0][1], 2)}
             for s, t in CURVE_TENORS if seqs.get(s)]
    cur   = lambda s: seqs[s][0][1] if seqs.get(s) else None
    t2, t3 = cur("T10Y2Y"), cur("T10Y3M")
    inversion = {
        "t10y2y": round(t2, 2) if t2 is not None else None,
        "t10y3m": round(t3, 2) if t3 is not None else None,
        "inverted": bool((t2 is not None and t2 < 0) or (t3 is not None and t3 < 0)),
    }
    return {"fred_keyed": True, "as_of": as_of, "rates": rows, "curve": curve,
            "inversion": inversion,
            "note": "Treasury yields, fed funds & curve spreads from FRED (daily). Δ in basis points."}


def event_risk(horizon=10, alert_days=2):
    """The signal hook: the soonest high-impact macro event within `horizon`
    days. `flag` trips when it is <= `alert_days` out (event-risk: smaller,
    tighter into the print). Fail-soft → flag False, event None."""
    try:
        ensure_fresh()
        today = date.today()
        rows  = store.get_events(
            today.strftime("%Y-%m-%d"),
            (today + timedelta(days=horizon)).strftime("%Y-%m-%d"),
            importance=["high"], kinds=list(HIGH_KINDS),
        )
        if not rows:
            return {"flag": False, "event": None, "days_until": None, "note": ""}
        nxt = min(rows, key=lambda e: (e["event_date"], -IMPORTANCE_RANK.get(e["importance"], 0)))
        ev  = _decorate(nxt, today)
        du  = ev["days_until"]
        note = f"{ev['title']} {ev['when']}"
        if du <= alert_days:
            note += " — event risk: size smaller, tighten stops into the print"
        return {"flag": du <= alert_days, "event": ev, "days_until": du, "note": note}
    except Exception:
        return {"flag": False, "event": None, "days_until": None, "note": ""}


def summary():
    """Hub badge: next high-impact macro event."""
    er = event_risk(horizon=14, alert_days=2)
    ev = er.get("event")
    if not ev:
        return {"text": "no events", "status": "neutral", "flag": False}
    short = (ev["title"].replace("FOMC rate decision", "FOMC")
                        .replace("Employment Situation (Nonfarm Payrolls)", "Jobs")
                        .replace("Consumer Price Index (CPI)", "CPI")
                        .replace("Gross Domestic Product (GDP)", "GDP")
                        .replace("Personal Income & Outlays (PCE)", "PCE"))
    text = f"{short} {ev['when']}"
    return {"text": text, "status": ("accent" if er["flag"] else "ok"), "flag": er["flag"]}
