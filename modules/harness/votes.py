"""
Vote adapters — turn each data module's existing signal into one normalized,
signed VOTE the combiner can fold together (Notes.txt step 3).

Every adapter:
  * reaches UP to its module via a LAZY, in-function, fail-soft import (the
    canslim/flow precedent — the harness is the absolute top consumer, so it must
    not create module-load import edges and must never crash when a module's data
    isn't ready);
  * REUSES the module's existing entrypoint (no new quant lives here);
  * returns the shared Vote dict, or a `ok=False` stub on any failure.

Vote schema:
  { domain, scope, direction (+1/0/-1), conviction (0..100), weight,
    factors [[label, amount], ...], horizon, regime_context, rationale, ok, note,
    detail {...} }

`weight` is theory-fixed domain importance (breadth = the regime arbiter, highest;
themes/screener = lowest). The combiner multiplies direction × conviction × weight.
`detail` carries the per-scope payload the combiner's sector-confluence and the
LLM brief read; it never enters the scalar math.
"""

# Theory-fixed domain weights (judgment, not searched) — breadth (regime arbiter)
# dominates, leadership/rotation next, flow/news mid, screener/themes lowest.
WEIGHTS = {
    "breadth":  30,
    "rrg":      20,
    "rankings": 15,
    "canslim":  15,
    "news":     12,
    "flow":     10,
    "screener":  8,
    "themes":    6,
}

_HORIZON = {
    "breadth": "position", "rrg": "swing", "rankings": "swing", "canslim": "position",
    "news": "event", "flow": "swing", "screener": "intraday", "themes": "position",
}

# Offensive vs defensive SPDR sectors — leadership tilt = a risk-on/off read.
_OFFENSIVE = {"XLK", "XLY", "XLF", "XLI", "XLC"}
_DEFENSIVE = {"XLU", "XLP", "XLV"}


def _vote(domain, direction, conviction, *, scope="market", factors=None,
          detail=None, regime_context=None, ok=True, note=None):
    return {
        "domain":         domain,
        "scope":          scope,
        "direction":      int(direction),
        "conviction":     round(float(max(0.0, min(100.0, conviction))), 1),
        "weight":         WEIGHTS.get(domain, 0),
        "factors":        factors or [],
        "horizon":        _HORIZON.get(domain, "swing"),
        "regime_context": regime_context,
        "rationale":      None,          # filled by the LLM layer (agents.py), optional
        "ok":             bool(ok),
        "note":           note,
        "detail":         detail or {},
    }


def _fail(domain, e):
    return _vote(domain, 0, 0, ok=False, note=str(e))


def _sign(x):
    return 1 if x > 1e-9 else (-1 if x < -1e-9 else 0)


def _dir_sign(raw):
    """Normalize a direction to +1/0/-1. Some modules (flow) store it as the
    string 'bullish'/'bearish'; others as a signed number."""
    if isinstance(raw, str):
        return {"bullish": 1, "bearish": -1}.get(raw.lower(), 0)
    try:
        return _sign(float(raw))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Per-domain adapters
# ---------------------------------------------------------------------------

def vote_breadth():
    """The regime ARBITER. HEALTHY → bullish, DETERIORATING → bearish, NEUTRAL → 0.
    Conviction scales with the regime score magnitude."""
    try:
        from modules.breadth import build_summary
        s = build_summary("sp500")
        reg = (s.get("regime") or "").upper()
        if reg not in ("HEALTHY", "NEUTRAL", "DETERIORATING"):
            return _fail("breadth", "no regime (run a breadth backfill)")
        direction = {"HEALTHY": 1, "DETERIORATING": -1}.get(reg, 0)
        score = abs(float(s.get("score") or 0))
        conv  = 40 + score * 18 if direction else 20
        factors = [[r, direction or 1] for r in (s.get("reasons") or [])[:4]]
        return _vote("breadth", direction, conv, factors=factors,
                     regime_context=reg, detail={
                         "regime": reg, "score": s.get("score"),
                         "interpretation": s.get("interpretation"),
                         "metrics": s.get("metrics"),
                         "divergences": s.get("active_divergences"),
                     })
    except Exception as e:
        return _fail("breadth", e)


def vote_rrg():
    """Sector rotation. Market direction = net bullish-call conviction vs
    bearish-call conviction across the 11 SPDR sectors. Per-sector calls ride in
    `detail` for the combiner's sector-confluence + the brief."""
    try:
        from modules.rrg import compute_rrg
        from modules.rrg.signal import DEFAULT_TICKERS, BENCHMARK
        from modules.harness.combiner import _RRG_BULL, _RRG_BEAR
        r = compute_rrg(DEFAULT_TICKERS, BENCHMARK, "1d")
        secs = r.get("sectors") or {}
        if not secs:
            return _fail("rrg", "no sector data")
        net = called = 0.0
        rows, in_n, out_n = [], 0, 0
        for t, d in secs.items():
            call = d.get("call")
            conv = float(d.get("conviction") or 0.0)
            if call in _RRG_BULL:
                net += conv; called += 1; in_n += int(call == "ROTATE IN")
            elif call in _RRG_BEAR:
                net -= conv; called += 1; out_n += int(call in _RRG_BEAR)
            rows.append({"ticker": t, "name": d.get("name"), "call": call,
                         "conviction": round(conv, 1)})
        direction = _sign(net)
        conv = min(100.0, abs(net) / max(1.0, called))
        factors = [[f"{rw['ticker']} {rw['call']}", rw["conviction"] * (1 if rw["call"] in _RRG_BULL else -1)]
                   for rw in sorted(rows, key=lambda x: abs(x["conviction"]), reverse=True)[:4]
                   if rw["call"] in _RRG_BULL or rw["call"] in _RRG_BEAR]
        return _vote("rrg", direction, conv, factors=factors,
                     regime_context=r.get("rotation"), detail={
                         "sectors": rows, "rotation": r.get("rotation"),
                         "regime": r.get("regime"), "in_count": in_n, "out_count": out_n,
                         "best": r.get("best"),
                     })
    except Exception as e:
        return _fail("rrg", e)


def vote_rankings():
    """Leadership tilt. Offensive sectors (tech/discretionary/financials/…) leading
    defensives (utilities/staples/health) = risk-on; the mirror = risk-off."""
    try:
        from modules.rankings import compute_rankings
        r = compute_rankings()
        secs = [s for s in (r.get("sectors") or []) if s.get("rank") is not None]
        if not secs:
            return _fail("rankings", "no rankings (build a screener snapshot)")
        off = [s["rank"] for s in secs if s["ticker"] in _OFFENSIVE]
        dfn = [s["rank"] for s in secs if s["ticker"] in _DEFENSIVE]
        if not off or not dfn:
            return _fail("rankings", "incomplete sector set")
        spread = (sum(off) / len(off)) - (sum(dfn) / len(dfn))
        direction = _sign(spread)
        conv = min(100.0, abs(spread) * 1.4)
        top = sorted(secs, key=lambda s: s["rank"], reverse=True)[:5]
        factors = [["offensive leadership" if direction > 0 else "defensive leadership",
                    round(spread, 1)]]
        return _vote("rankings", direction, conv, factors=factors, detail={
            "sectors": secs, "top": [{"ticker": s["ticker"], "name": s["name"],
                                      "rank": s["rank"]} for s in top],
            "offensive_avg": round(sum(off) / len(off), 1),
            "defensive_avg": round(sum(dfn) / len(dfn), 1),
        })
    except Exception as e:
        return _fail("rankings", e)


def vote_canslim():
    """Growth-leadership strength. Bullish when several names score high on the
    O'Neil 7-factor composite (the composite already folds the market regime)."""
    try:
        from modules.canslim import compute_canslim
        r = compute_canslim(limit=10)
        stocks = r.get("stocks") or []
        if not stocks:
            return _fail("canslim", "no scored stocks (needs fundamentals)")
        n_strong = sum(1 for s in stocks if (s.get("composite") or 0) >= 70)
        top = stocks[0]
        direction = 1 if n_strong >= 3 else (1 if (top.get("composite") or 0) >= 65 else 0)
        conv = min(100.0, n_strong * 11 + max(0, (top.get("composite") or 0) - 50))
        factors = [[f"{s['symbol']} {s['composite']}", 1] for s in stocks[:4]]
        return _vote("canslim", direction, conv, factors=factors,
                     regime_context=(r.get("market") or {}).get("regime"), detail={
                         "leaders": [{"symbol": s["symbol"], "composite": s["composite"],
                                      "sector": s.get("sector")} for s in stocks[:6]],
                         "n_strong": n_strong, "market": r.get("market"),
                     })
    except Exception as e:
        return _fail("canslim", e)


def vote_flow():
    """Whale options flow. Direction from the lead conviction signal; conviction
    from its strength (or the count of conviction-grade signals)."""
    try:
        from datetime import date
        from modules.flow import store as flow_store
        d = flow_store.latest_signal_date(date.today().strftime("%Y-%m-%d"))
        if not d:
            return _fail("flow", "no flow signals yet")
        top = flow_store.list_flow_signals(d, classification="conviction", limit=1)
        notable = flow_store.list_flow_signals(d, min_conviction=0, limit=300)
        n_conv = sum(1 for s in notable if s.get("classification") == "conviction")
        if not top:
            return _vote("flow", 0, min(60.0, n_conv * 8), detail={
                "lead": None, "conviction_count": n_conv, "date": d})
        t = top[0]
        direction = _dir_sign(t.get("direction"))
        conv = float(t.get("conviction") or 0)
        factors = [[f"{t['underlying']} whale {'calls' if direction > 0 else 'puts'}", direction or 1]]
        return _vote("flow", direction, conv, factors=factors, scope=t.get("underlying"),
                     detail={"lead": {"underlying": t.get("underlying"),
                                      "direction": t.get("direction"),
                                      "conviction": t.get("conviction")},
                             "conviction_count": n_conv, "date": d})
    except Exception as e:
        return _fail("flow", e)


def vote_news():
    """Event-risk GATE (a damper, not a directional bet). Bearish/cautionary when a
    high-impact macro print is imminent, or the yield curve is inverted."""
    try:
        from modules.news import calendar as news_cal
        er = news_cal.event_risk(horizon=10, alert_days=2)
        factors, bearish = [], 0.0
        ev = er.get("event")
        if ev:
            du = er.get("days_until")
            factors.append([f"{ev.get('title')} ({du}d)", -1])
            if er.get("flag"):
                bearish += 60                      # imminent print → size down
            elif du is not None:
                bearish += max(0, 30 - du * 3)     # mild as it approaches
        try:
            inv = (news_cal.build_rates().get("inversion") or {})
            if inv.get("inverted"):
                bearish += 25
                factors.append(["yield curve inverted", -1])
        except Exception:
            pass
        direction = -1 if bearish > 0 else 0
        return _vote("news", direction, min(100.0, bearish), factors=factors, detail={
            "event": ev, "days_until": er.get("days_until"),
            "flag": er.get("flag"), "note": er.get("note")})
    except Exception as e:
        return _fail("news", e)


# Screener alert rule_key keywords → a coarse risk-on / risk-off read.
_SCR_BULL = ("breakout", "building", "thrust", "gap_up", "high_break", "golden")
_SCR_BEAR = ("dump", "overbought", "stretch", "gap_down", "low_break", "climax")


def vote_screener():
    """Setup activity. A mild risk gauge from unacknowledged alerts: bullish setup
    keys (breakout/building/thrust) vs reversal-warning keys (dump/overbought)."""
    try:
        from modules.screener import store as scr_store
        a = scr_store.alerts_summary()
        by_sym = a.get("by_symbol") or {}
        if not by_sym:
            return _vote("screener", 0, 15, detail={"today": a.get("today", 0),
                                                    "unacked": a.get("unacked", 0),
                                                    "n_symbols": 0})
        bull = bear = 0
        for kinds in by_sym.values():
            for k in kinds:
                key = (k.get("rule_key") or "") + " " + (k.get("kind") or "")
                key = key.lower()
                if any(w in key for w in _SCR_BULL):
                    bull += 1
                elif any(w in key for w in _SCR_BEAR):
                    bear += 1
        direction = _sign(bull - bear)
        conv = min(60.0, abs(bull - bear) * 6 + 15)
        return _vote("screener", direction, conv,
                     factors=[[f"{bull} bullish / {bear} bearish alerts", direction or 1]],
                     detail={"today": a.get("today", 0), "unacked": a.get("unacked", 0),
                             "n_symbols": len(by_sym), "bull": bull, "bear": bear})
    except Exception as e:
        return _fail("screener", e)


def vote_themes():
    """Thematic leadership. Strong top-theme rank vs SPY = risk appetite for the
    speculative end of the tape. Lowest weight."""
    try:
        from modules.themes import compute_theme_view
        secs = (compute_theme_view("daily", 6).get("ranking") or {}).get("sectors") or []
        secs = [s for s in secs if s.get("rank") is not None]
        if not secs:
            return _fail("themes", "no themes")
        top = secs[0]
        spread = top["rank"] - 50
        direction = _sign(spread)
        conv = min(60.0, abs(spread) * 1.4)
        return _vote("themes", direction, conv,
                     factors=[[f"{top.get('name')} rank {top['rank']}", direction or 1]],
                     detail={"top": [{"name": s.get("name"), "rank": s.get("rank")}
                                     for s in secs[:5]]})
    except Exception as e:
        return _fail("themes", e)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_ADAPTERS = [vote_breadth, vote_rrg, vote_rankings, vote_canslim,
             vote_flow, vote_news, vote_screener, vote_themes]


def gather_all():
    """Run every adapter (fail-soft) and resolve the canonical regime + rotation
    (from rrg.signal — cached) used by the combiner's arbitration.

    Returns (votes, regime, rotation)."""
    votes = []
    for fn in _ADAPTERS:
        try:
            votes.append(fn())
        except Exception as e:                     # an adapter should never raise, but belt-and-braces
            votes.append(_fail(fn.__name__.replace("vote_", ""), e))

    regime = rotation = None
    try:
        from modules.rrg import signal as rrg_signal
        regime   = rrg_signal.current_regime()
        rotation = rrg_signal.rotation_regime()
    except Exception:
        pass
    # fall back to the breadth vote's regime if signal couldn't resolve it
    if regime is None:
        for v in votes:
            if v["domain"] == "breadth" and v["ok"]:
                regime = (v.get("detail") or {}).get("regime")
                break
    return votes, regime, rotation
