"""
CANSLIM module — William O'Neil / IBD's 7-factor growth-stock scorecard, served at
`/canslim.html`. Like Themes, this is **composition, not new quant**: each letter is
read from a signal the harness already produces, blended into a 0-99 composite.

  C — Current quarterly EPS growth      ← screener fundamentals `eps_growth_q`
  A — Annual earnings growth            ← screener fundamentals `eps_growth_a`
  N — New high / new product            ← screener snapshot `pct_from_52w_high`
  S — Supply & demand                   ← confluence `ad_rating` (demand) + float (supply)
  L — Leader vs laggard (RS rating)     ← cross-sectional percentile of the snapshot RS
                                          (`rankings._percentile_mapper`, the IBD RS-Rating idea)
  I — Institutional sponsorship         ← confluence `institutional` leaf over the screener's
                                          inst-ownership history (% held + holder-count QoQ trend;
                                          None until two dated reads accumulate)
  M — Market direction                  ← breadth regime (`signal.current_regime()`), a
                                          per-stock multiplier + a banner, not a column

Each letter is scored 0-100; the composite is a weighted average over the *available*
letters × the market factor → 0-99. Fail-soft throughout: a missing letter is dropped
from the blend (so the page works before fundamentals finish syncing). Data freshness
caveats mirror the screener's (latest-known fundamentals, today's universe membership).
"""

import math
from pathlib import Path

from modules import Response
from modules.rrg import signal
from modules.rankings import _percentile_mapper
from modules.confluence import institutional

_MODULE_DIR = Path(__file__).resolve().parent

# CANSLIM thresholds & weights (judgment/theory-fixed, not searched) -----------
GROWTH_TARGET = 0.25     # O'Neil's +25% earnings-growth bar → a strong (not max) score
NEAR_HIGH_FULL = 5.0     # within 5% of the 52w high = full N score
NEAR_HIGH_ZERO = 30.0    # ≥30% below the high = zero N score
PASS_SCORE = 60.0        # a letter "passes" at ≥ this
RS_BLEND = (("rs_1m_pct", 0.5), ("rs_3m_pct", 0.5))   # the RS composite for L

LETTER_WEIGHTS = {"C": 0.20, "A": 0.15, "N": 0.15, "S": 0.15, "L": 0.25, "I": 0.10}
AD_SCORE = {"A": 100.0, "B": 75.0, "C": 50.0, "D": 25.0, "E": 0.0}

# Market-direction multiplier — O'Neil: ~3 of 4 stocks follow the market, so a great
# setup in a downtrend is dampened (don't fight the tape).
MARKET_FACTOR = {"HEALTHY": 1.0, "NEUTRAL": 0.85, "DETERIORATING": 0.6}

LEADERBOARD_LIMIT = 50
DEFAULT_UNIVERSE = "sp500"

CAVEAT = ("Composition of existing signals (latest-known fundamentals, not point-in-time; "
          "today's universe membership). Needs a built screener snapshot + fundamentals; "
          "missing letters are dropped from the blend. 'I' (institutional) is forward-"
          "accumulating — it scores only once two dated ownership reads exist. "
          "Not investment advice.")


# ---------------------------------------------------------------------------
# Pure letter scorers (unit-tested) — each returns 0-100 or None (unavailable)
# ---------------------------------------------------------------------------

def _growth_score(g, target=GROWTH_TARGET):
    """Earnings-growth fraction (0.25 = +25%) → 0-100. 0 at ≤0 growth, ~75 at the
    target, asymptoting to 100 for very strong growth. None when unavailable."""
    if g is None or (isinstance(g, float) and math.isnan(g)):
        return None
    if g <= 0:
        return 0.0
    # 75 at the target, then diminishing returns toward 100
    return float(min(100.0, 75.0 * (g / target) ** 0.6))


def _near_high_score(pct_from_high):
    """`pct_from_52w_high` (≤0; 0 = at the high) → 0-100. Full within NEAR_HIGH_FULL%
    of the high, linearly to 0 by NEAR_HIGH_ZERO% below. None when unavailable."""
    if pct_from_high is None or (isinstance(pct_from_high, float) and math.isnan(pct_from_high)):
        return None
    dist = abs(float(pct_from_high))            # distance below the high, in %
    if dist <= NEAR_HIGH_FULL:
        return 100.0
    if dist >= NEAR_HIGH_ZERO:
        return 0.0
    return float(100.0 * (NEAR_HIGH_ZERO - dist) / (NEAR_HIGH_ZERO - NEAR_HIGH_FULL))


def _supply_demand_score(ad_rating, float_pctl=None):
    """Demand from the accumulation rating (A-E) + a small-float supply bonus.
    `float_pctl` is the symbol's shares-outstanding percentile among the universe
    (smaller float = scarcer supply = better). None ad_rating → None."""
    if not ad_rating or ad_rating not in AD_SCORE:
        return None
    demand = AD_SCORE[ad_rating]
    if float_pctl is None:
        return demand
    supply = 100.0 - float(float_pctl)          # smaller float ⇒ higher
    return float(0.75 * demand + 0.25 * supply)


def _market_factor(regime):
    return MARKET_FACTOR.get((regime or "").upper(), 0.85)


def _composite(scores):
    """Weighted blend of the available letter scores (`{letter: score|None}`) over
    LETTER_WEIGHTS, renormalized to the present letters. None when nothing scored."""
    num = den = 0.0
    for k, w in LETTER_WEIGHTS.items():
        s = scores.get(k)
        if s is not None:
            num += w * s
            den += w
    if den <= 0:
        return None
    return num / den


def score_stock(row, rs_rating, float_pctl=None, inst_read=None):
    """Assemble one stock's 7-letter scorecard from a merged snapshot+fundamentals
    `row` (dict-like) + its precomputed cross-sectional `rs_rating` (L) + an optional
    institutional read (`institutional.current(...)` dict, the I letter). Returns
    {letters: {C:{score,pass}, ...}, composite_raw} — the market factor is applied by
    the caller (it's shared across stocks)."""
    letters = {
        "C": _growth_score(row.get("eps_growth_q")),
        "A": _growth_score(row.get("eps_growth_a")),
        "N": _near_high_score(row.get("pct_from_52w_high")),
        "S": _supply_demand_score(row.get("ad_rating"), float_pctl),
        "L": float(rs_rating) if rs_rating is not None else None,
        "I": institutional.score(inst_read),    # None until ownership data accumulates
    }
    out = {k: ({"score": round(v, 1), "pass": v >= PASS_SCORE} if v is not None else None)
           for k, v in letters.items()}
    return {"letters": out, "composite_raw": _composite(letters)}


# ---------------------------------------------------------------------------
# Composition over the universe
# ---------------------------------------------------------------------------

def _rs_value(row):
    """Cross-sectional RS composite for one row (blend of the snapshot RS columns)."""
    num = den = 0.0
    for col, w in RS_BLEND:
        v = row.get(col)
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            num += w * float(v); den += w
    return num / den if den > 0 else None


def compute_canslim(universe=DEFAULT_UNIVERSE, limit=LEADERBOARD_LIMIT):
    """Build the CANSLIM leaderboard for `universe`. Reads the screener snapshot +
    fundamentals (built by the screener's background jobs) and the breadth regime;
    composes the 7-letter scorecard per stock. Fail-soft → an empty board + a note."""
    import numpy as np
    from modules.screener import store as scr_store
    try:
        from modules.breadth import store as breadth_store
    except Exception:
        breadth_store = None

    snap = scr_store.get_snapshot()
    if snap is None or snap.empty:
        return {"stocks": [], "market": _market_block(), "as_of": None,
                "universe": universe, "note": "No screener snapshot yet — run a screener sync."}

    df = snap.join(scr_store.get_fundamentals(), how="left")
    if universe != "all" and breadth_store is not None:
        members = set(breadth_store.get_members(universe))
        if members:
            df = df[df.index.isin(members)]
    if df.empty:
        return {"stocks": [], "market": _market_block(), "as_of": None,
                "universe": universe, "note": "No symbols in this universe yet."}

    # cross-sectional RS rating (L) — IBD-style percentile vs the whole universe
    rs_vals = df.apply(lambda r: _rs_value(r), axis=1)
    rs_map = _percentile_mapper(rs_vals.to_numpy(dtype=float))
    # shares-outstanding percentile for the small-float supply bonus (S)
    so = df.get("shares_outstanding")
    so_map = _percentile_mapper(so.to_numpy(dtype=float)) if so is not None else (lambda v: None)
    # institutional ownership (I) — current + prior-quarter reads, fail-soft
    try:
        own = scr_store.get_inst_ownership()
    except Exception:
        own = None

    market = _market_block()
    factor = market["factor"]

    stocks = []
    for sym, row in df.iterrows():
        r = row.to_dict()
        rs_rating = rs_map(rs_vals.get(sym))
        float_pctl = so_map(r.get("shares_outstanding")) if so is not None else None
        inst_read = None
        if own is not None and sym in own.index:
            o = own.loc[sym]
            inst_read = institutional.current(o.get("pct_held"), o.get("pct_held_prev"),
                                              o.get("holders_count"), o.get("holders_count_prev"))
        card = score_stock(r, rs_rating, float_pctl, inst_read)
        if card["composite_raw"] is None:
            continue
        composite = int(min(99, max(0, round(card["composite_raw"] * factor))))
        stocks.append({
            "symbol":    sym,
            "composite": composite,
            "letters":   card["letters"],
            "rs_rating": rs_rating,
            "ad_rating": _safe(r.get("ad_rating")),
            "pct_from_52w_high": _safe(r.get("pct_from_52w_high")),
            "eps_growth_q": _safe(r.get("eps_growth_q")),
            "eps_growth_a": _safe(r.get("eps_growth_a")),
            "sector":    _safe(r.get("sector")),
            "close":     _safe(r.get("close")),
        })

    stocks.sort(key=lambda s: s["composite"], reverse=True)
    as_of = str(df["date"].dropna().max()) if "date" in df.columns and df["date"].notna().any() else None
    return {"stocks": stocks[:limit], "market": market, "as_of": as_of,
            "universe": universe, "total": len(stocks), "note": CAVEAT}


def _market_block():
    try:
        regime = signal.current_regime()
    except Exception:
        regime = None
    return {"regime": regime, "factor": _market_factor(regime),
            "label": _market_label(regime)}


def _market_label(regime):
    return {
        "HEALTHY":       "Uptrend — full position sizing (the wind is at your back)",
        "NEUTRAL":       "Mixed — be selective, lighter sizing",
        "DETERIORATING": "Downtrend — even great setups dampened; mostly cash",
    }.get((regime or "").upper(), "Market regime unknown")


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


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_page(req):
    with open(_MODULE_DIR / "canslim.html") as f:
        return Response.html(f.read())


def _handle_api(req):
    universe = req.qs.get("universe", [DEFAULT_UNIVERSE])[0]
    try:
        limit = int(req.qs.get("limit", [str(LEADERBOARD_LIMIT)])[0])
    except (TypeError, ValueError):
        limit = LEADERBOARD_LIMIT
    limit = max(5, min(limit, 200))
    return Response.json(compute_canslim(universe, limit))


def _handle_summary(req):
    """Hub badge: the top CANSLIM name + its composite (fail-soft)."""
    try:
        rep = compute_canslim(DEFAULT_UNIVERSE, limit=1)
        top = rep["stocks"][0] if rep["stocks"] else None
        if not top:
            return Response.json({"text": None, "status": "neutral"})
        return Response.json({"text": f"{top['symbol']} {top['composite']}",
                              "status": "ok", "regime": rep["market"]["regime"]})
    except Exception:
        return Response.json({"text": None, "status": "neutral"})


def register_routes(router):
    router.get("/canslim.html", _handle_page)
    router.get("/api/canslim", _handle_api)
    router.get("/api/canslim/summary", _handle_summary)
