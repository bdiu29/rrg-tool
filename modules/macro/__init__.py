"""
Macro module — the market-health / regime backend, served at `/macro.html` and
consumed by the harness page (which is the unified Livermore-style dashboard).

It owns the genuinely-new quant the harness was missing: a deterministic
growth×inflation 4-quadrant regime classifier (`regime.py`) plus the Leading +
Macro "signals of health" panels (`indicators.py`), built from FRED + yfinance +
the breadth module's internals (`sources.py`). Everything is deterministic and
$0 — the LLM only interprets it on the harness page.

Dependency position: a mid/high CONSUMER (reads breadth / news-FRED / rrg-price via
LAZY, fail-soft imports, exactly like canslim); only the harness imports it back.

Routes:
  GET  /macro.html          → the standalone detail page
  GET  /api/macro           → the full dashboard (regime + panels + health)
  GET  /api/macro/summary   → hub/harness badge (regime + confidence; cache-only)
  POST /api/macro/refresh   → force a re-fetch
"""

from pathlib import Path

from modules import Response
from modules.macro import sources, indicators, regime

_MODULE_DIR = Path(__file__).resolve().parent

_NOTE = ("Regime is a deterministic growth×inflation read from market + FRED proxies; "
         "probabilities reflect current positioning, not a forecast. Indicator meanings "
         "are templated (instant, $0); the harness LLM adds the interpretation. "
         "Educational only, not investment advice.")


def _market_health(raw):
    """The simple 'Market Health' stat block (SPY vs its trends + breadth %s)."""
    close = raw["close"]
    spy = sources.col(close, "SPY")
    b = (raw.get("breadth") or {}).get("metrics") or {}
    out = {"pct_above_50": b.get("pct_above_50"), "pct_above_200": b.get("pct_above_200")}

    if spy is not None and len(spy) > 50:
        last = float(spy.iloc[-1])
        def _vs(window, span=None):
            if span:
                ema = spy.ewm(span=span, adjust=False).mean()
                base = float(ema.iloc[-1])
            else:
                if len(spy) < window:
                    return None
                base = float(spy.rolling(window).mean().iloc[-1])
            return round((last / base - 1) * 100, 2) if base else None
        out["spy_vs_50"]  = _vs(50)
        out["spy_vs_200"] = _vs(200)
        out["spy_vs_62w_ema"] = _vs(None, span=62 * 5)     # 62-week EMA in trading days
        # SPY YTD (first bar of the current calendar year).
        yr = spy.index[-1].year
        ytd_base = spy[spy.index.year == yr]
        if len(ytd_base):
            out["spy_ytd"] = round((last / float(ytd_base.iloc[0]) - 1) * 100, 2)
    return out


def _build_cards(reg, health, panels):
    """The 'What You Need to Know' Q&A cards (the Livermore set) — deterministic
    plain-English reads off the regime + health + indicators. Instant + $0; the LLM's
    interpretation lives in the harness brief ABOVE the cards. Each card is a
    {key, title, headline, body}."""
    regime_name = reg.get("regime")
    conf  = reg.get("confidence") or 0
    shift = reg.get("shift_risk") or "Moderate"
    tilt  = reg.get("equity_tilt") or 0.0
    probs = reg.get("probabilities") or []
    axes  = reg.get("axes") or ""
    spy50 = health.get("spy_vs_50")
    p50   = health.get("pct_above_50")
    cards = []

    # 1 — Where are we (the regime read)
    if tilt > 0.3:
        h = "Constructive, but stay alert."
    elif tilt < -0.3:
        h = "Defensive — capital preservation first."
    else:
        h = "Murky, but leaning %s." % ("good" if tilt >= 0 else "cautious")
    body = (f"The market is most likely in a '{regime_name}' regime ({axes}), but it's a {conf}% "
            f"read with {shift.lower()} risk of a shift. "
            + ("Consider staying invested while watching how the incoming data prints." if tilt >= 0
               else "Consider defending capital and keeping size small until the picture clears."))
    cards.append({"key": "where", "title": "Where Are We", "headline": h, "body": body})

    # 2 — Too late to buy (extension vs the 50-day)
    if spy50 is None:
        h, body = "Hard to say.", "Not enough price history to judge how extended the market is right now."
    elif spy50 < 0:
        h = "Below trend."
        body = (f"The S&P 500 has slipped about {abs(spy50):.1f}% below its 50-day average — the "
                "short-term trend is broken. Consider waiting for it to stabilize before adding.")
    elif spy50 > 10:
        h = "Stretched."
        body = (f"The S&P 500 sits about {spy50:.1f}% above its 50-day average — extended. Consider "
                "letting pullbacks come to you rather than chasing up here.")
    else:
        h = "Not extended, not cheap."
        body = (f"The S&P 500 sits about {spy50:.1f}% above its 50-day average — a neutral zone, not "
                "dangerously overbought. Consider treating dips as normal pauses, not warnings.")
    cards.append({"key": "too_late", "title": "Too Late to Buy?", "headline": h, "body": body})

    # 3 — Buy the dip (breadth participation)
    if p50 is None:
        h, body = "Be selective.", "Breadth data isn't available — focus on the strongest names."
    elif p50 >= 60:
        h = "Dips are buyable."
        body = (f"With {p50:.0f}% of stocks above their 50-day line, participation is broad. Consider "
                "buying dips in the leaders — the rally has support under the surface.")
    elif p50 >= 45:
        h = "Buy quality, not everything."
        body = (f"With breadth around {p50:.0f}%, only about half of stocks are participating, so the "
                "market isn't broadly healthy. Consider focusing any dip-buying on the strongest themes "
                "rather than reaching for laggards.")
    else:
        h = "Be choosy."
        body = (f"Only {p50:.0f}% of stocks are above their 50-day line — narrow. Consider being very "
                "selective; this isn't a buy-everything tape.")
    cards.append({"key": "buy_dip", "title": "Buy the Dip?", "headline": h, "body": body})

    # 4 — When does it end (regime duration)
    if regime_name in ("Goldilocks", "Reflation"):
        h = "Months, not weeks, typically."
        body = (f"Risk-on regimes historically run for months once established — but this one is "
                f"{'fragile' if shift != 'Low' else 'reasonably stable'} given {shift.lower()} shift "
                "risk. Consider reviewing positions every few weeks as the data evolves.")
    else:
        h = "Until the data turns."
        body = (f"Defensive regimes can persist for several months. Consider waiting for growth and "
                f"risk-appetite signals to turn back up before adding risk — shift risk is {shift.lower()}.")
    cards.append({"key": "when_end", "title": "When Does It End", "headline": h, "body": body})

    # 5 — Hidden risk (the non-obvious one): a regime flip, else a complacency tell
    caution = [r for r in (panels.get("leading", []) + panels.get("macro", []))
               if r.get("state") in ("COMPLACENT", "TURNED")]
    if shift == "High" and len(probs) >= 2:
        top1, top2 = probs[0], probs[1]
        margin = top1["prob"] - top2["prob"]
        hedge = ("real assets or energy" if top2["name"] in ("Reflation", "Stagflation")
                 else "defensives or bonds")
        h = "A regime flip few are pricing."
        body = (f"The gap between {top1['name']} and {top2['name']} odds is just {margin} points — a "
                f"couple of surprising prints could reprice the whole market toward {top2['name']}. "
                f"Consider keeping some exposure to {hedge} as a quiet hedge.")
    elif caution:
        c = caution[0]
        h = "Complacency is the risk."
        body = (f"{c['label']} is flashing {c['state'].lower()}: {c['meaning']} Consider trimming risk "
                "into strength rather than adding.")
    else:
        h = "No glaring hidden risk."
        body = ("Nothing is obviously broken — which means the biggest danger is complacency itself. "
                "Consider keeping stops honest and not over-sizing.")
    cards.append({"key": "hidden", "title": "Hidden Risk", "headline": h, "body": body})

    return cards


def build_dashboard(force=False):
    """Fetch (TTL-cached) → features → regime → panels → health → cards. The compute is
    milliseconds; only `sources.fetch_raw` touches the network and it self-caches."""
    raw   = sources.fetch_raw(force=force)
    feats = indicators.regime_features(raw)
    reg   = regime.classify(feats)
    panels = indicators.build_indicators(raw)
    health = _market_health(raw)

    return {
        "as_of":   raw.get("as_of"),
        "ok":      raw.get("ok"),
        "regime":  reg,
        "features": feats,
        "leading": panels["leading"],
        "macro":   panels["macro"],
        "leading_summary": panels["leading_summary"],
        "macro_summary":   panels["macro_summary"],
        "deferred": panels["deferred"],
        "health":  health,
        "cards":   _build_cards(reg, health, panels),
        "note":    _NOTE,
    }


def summary():
    """Badge read — uses the warm raw cache only (never forces a network fetch on
    the homepage / harness-badge path). Returns a 'cold' stub if nothing is cached."""
    if sources._CACHE.get("data") is None:
        return {"text": "not loaded", "status": "neutral", "cold": True}
    d = build_dashboard(force=False)
    r = d["regime"]
    return {"text": f"{r['regime']} · {r['confidence']}%",
            "status": "ok" if r["equity_tilt"] >= 0 else "accent",
            "regime": r["regime"], "confidence": r["confidence"],
            "shift_risk": r["shift_risk"]}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _handle_page(req):
    with open(_MODULE_DIR / "macro.html") as f:
        return Response.html(f.read())


def _handle_api(req):
    try:
        return Response.json(build_dashboard(force=False))
    except Exception as e:
        return Response.error(str(e))


def _handle_summary(req):
    try:
        return Response.json(summary())
    except Exception:
        return Response.json({"text": "unavailable", "status": "neutral"})


def _handle_refresh(req):
    try:
        return Response.json(build_dashboard(force=True))
    except Exception as e:
        return Response.error(str(e))


def register_routes(router):
    router.get("/macro.html",          _handle_page)
    router.get("/api/macro",           _handle_api)
    router.get("/api/macro/summary",   _handle_summary)
    router.post("/api/macro/refresh",  _handle_refresh)
