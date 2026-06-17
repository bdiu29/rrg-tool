"""
Harness combiner — the cross-agent `_conviction`, lifted from the factor level to
the MODULE level.

This is the alpha-bearing core, and it is deliberately **LLM-free and
deterministic** so the existing `rrg/backtest.py` referee can replay/score it
later (Notes.txt step 5) and so the harness still produces a decision at $0 with
no network. Each domain module publishes a signed VOTE (see `votes.py`); this file
sums them — exactly the signed `add(amt, label)` pattern of `signal._conviction` —
and arbitrates the result by regime into a CONCENTRATE vs ROTATE stance.

The validated finding (Notes.txt / the subagent-harness-plan memory): no single
module beats a concentration regime, so the edge is confluence + regime
arbitration — "concentrate when breadth is narrow, rotate when broad", NOT "beat
the regime". The stance arbitration below encodes exactly that.
"""

# Composite score clamp (its own symmetric scale, unlike signal.py's asymmetric
# display clamp — this is a fresh market-level read, not the RRG display coord).
SCORE_LO, SCORE_HI = -100.0, 100.0

# Posture buckets on the clamped composite (bullish + / bearish −).
POSTURE_BANDS = [
    (40,  "Risk-on"),
    (15,  "Lean bullish"),
    (-15, "Neutral / mixed"),
    (-40, "Lean bearish"),
    (-1e9, "Risk-off"),
]

STANCE_CONCENTRATE = "CONCENTRATE"
STANCE_ROTATE      = "ROTATE"
STANCE_NEUTRAL     = "NEUTRAL"


def decide_stance(regime, rotation):
    """The first-class regime arbiter (Notes.txt step 2). ROTATE only when breadth
    is broad AND equal-weight is leading cap-weight (rotation live); CONCENTRATE
    when breadth deteriorates OR rotation is off (a mega-cap concentration regime);
    NEUTRAL otherwise. Fail-soft: unknown inputs → NEUTRAL."""
    reg = (regime or "").upper()
    if reg == "HEALTHY" and rotation == "on":
        return STANCE_ROTATE
    if reg == "DETERIORATING" or rotation == "off":
        return STANCE_CONCENTRATE
    return STANCE_NEUTRAL


def _stance_factor(domain, stance):
    """Per-domain multiplier the stance applies LAST (mirrors the rotation gate in
    `signal._conviction`). In a concentration regime the RRG *rotation* bet is
    suppressed (it has no edge there — the validated finding) and leadership votes
    (rankings/canslim) are leaned into; in a rotation regime RRG runs at full
    weight."""
    if stance == STANCE_CONCENTRATE:
        if domain == "rrg":
            return 0.5
        if domain in ("rankings", "canslim"):
            return 1.2
    return 1.0


def _posture(score):
    for threshold, label in POSTURE_BANDS:
        if score >= threshold:
            return label
    return POSTURE_BANDS[-1][1]


def combine(votes, regime, rotation):
    """Sum signed weighted votes → a composite market read + the regime stance +
    the cross-domain confluence longs/avoids.

    Each vote contributes `direction × (conviction/100) × weight × stance_factor`.
    A vote with `ok=False` (the module had no data) is skipped — fail-soft, so the
    harness degrades gracefully as modules come online. Returns a plain dict (JSON
    serialisable, no numpy) so it can be cached/echoed directly."""
    stance  = decide_stance(regime, rotation)
    bull = bear = 0.0
    factors = []          # [domain, signed_amount] — the breakdown, like conviction_factors

    for v in votes:
        if not v.get("ok"):
            continue
        amt = (v.get("direction", 0)
               * (float(v.get("conviction", 0)) / 100.0)
               * float(v.get("weight", 0))
               * _stance_factor(v.get("domain", ""), stance))
        if abs(amt) <= 1e-9:
            continue
        if amt > 0:
            bull += amt
        else:
            bear += -amt
        factors.append([v.get("domain", "?"), round(amt, 1)])

    score = round(max(SCORE_LO, min(SCORE_HI, bull - bear)), 1)
    factors.sort(key=lambda f: abs(f[1]), reverse=True)

    longs, avoids = _sector_confluence(votes)

    return {
        "score":     score,
        "posture":   _posture(score),
        "stance":    stance,
        "regime":    regime,
        "rotation":  rotation,
        "factors":   factors,
        "bull":      round(bull, 1),
        "bear":      round(bear, 1),
        "n_votes":   sum(1 for v in votes if v.get("ok")),
        "longs":     longs,
        "avoids":    avoids,
    }


# RRG calls split into a bullish (add/hold) vs bearish (trim/avoid) tilt. The two
# extension warnings are CONTINUATION per the validated event study (w5-extended
# ≈ +1.9% @10d), so they count to the bullish side — NOT as exits.
_RRG_BULL = {"ROTATE IN", "HOLD", "⚠️ w3 extended", "⚠️ w5 extended"}
_RRG_BEAR = {"ROTATE OUT", "AVOID"}


def score_sectors(rrg_rows, rank_by, stance=None):
    """Per-sector confluence score — the SINGLE SOURCE OF TRUTH for both the live
    brief's longs/avoids (`_sector_confluence`) and the Phase-2 backtest's per-date
    confluence call (`harness.backtest`). On the SECTOR ETFs, the one scope RRG and
    rankings share directly (same XL* tickers — no fragile stock→sector mapping):

      score = RRG signed conviction (× the regime stance's rotation suppression)
            + a rank tilt (rank−50)
            ×1.25 if the RRG tilt and the rank tilt AGREE (confluence > a lone signal).

    `stance=None` ⇒ no rotation suppression (the live default — behavior unchanged);
    the backtest passes the per-date stance so CONCENTRATE halves the RRG rotation bet
    (the validated finding), exactly as the composite combiner does."""
    rrg_factor = _stance_factor("rrg", stance) if stance else 1.0
    scored = []
    for s in (rrg_rows or []):
        t    = s.get("ticker")
        call = s.get("call")
        conv = float(s.get("conviction") or 0.0)
        sign = 1 if call in _RRG_BULL else (-1 if call in _RRG_BEAR else 0)
        score = sign * conv * rrg_factor
        agree = False
        rk = rank_by.get(t)
        if rk is not None:
            score += (rk - 50) * 0.6          # above-median rank = positive tilt
            if sign != 0 and (sign > 0) == (rk >= 50):   # RRG + rank point the same way
                score *= 1.25
                agree = True
        scored.append({
            "ticker": t, "name": s.get("name"), "call": call,
            "conviction": round(conv, 1), "rank": rk,
            "score": round(score, 1), "agree": agree,
        })
    return scored


def _sector_confluence(votes):
    """Live-path longs/avoids from the vote details (no stance suppression — the v1
    behavior; the composite already carries the regime tilt)."""
    rrg  = _detail(votes, "rrg")
    rank = _detail(votes, "rankings")
    if not rrg:
        return [], []
    rank_by = {s["ticker"]: s.get("rank") for s in (rank.get("sectors") or [])
               if s.get("rank") is not None}
    scored = score_sectors(rrg.get("sectors") or [], rank_by)
    longs  = [x for x in sorted(scored, key=lambda x: x["score"], reverse=True)
              if x["score"] > 0][:5]
    avoids = [x for x in sorted(scored, key=lambda x: x["score"])
              if x["score"] < 0][:5]
    return longs, avoids


def _detail(votes, domain):
    for v in votes:
        if v.get("domain") == domain and v.get("ok"):
            return v.get("detail") or {}
    return {}
