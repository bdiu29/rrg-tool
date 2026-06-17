"""
Unusual options-flow scoring — the trader's 6-rule filter, encoded as pure
functions with named-constant gates (no I/O; unit-tested). This is the heart of
the module: ~90% of options flow is noise (hedges, spreads, rolls, exits), so the
job is to disqualify the noise and grade the small confirmed subset.

The rules (from a successful flow trader's SOP):
  1. Confirmed AGGRESSIVE buys (A/AA = at/above the ask). Schwab gives snapshots,
     not the trade tape, so the aggressor is ESTIMATED (last vs bid/ask) and stays
     ADVISORY — it never gates here. A Polygon trade-tape source flips this to a
     real gate by reporting confirmed A/AA (see `aggressor_method`).
  2. SIZE in context — premium notional vs an absolute floor AND vs the ticker's
     own typical daily options flow (a $1M order is huge on a thin name, noise on a
     mega-cap) AND vs existing open interest.
  3. VOL vs OI — volume below OI is churn (dropped); ≥5× is interesting, ≥20× is
     fresh money. The next-morning OI delta (entered vs exited) is handled by the
     poller, not here.
  4. FORM/urgency — repeated prints (clusters) on the same contract = accumulation.
     Sweeps/blocks need the trade tape (Polygon); Schwab can only see clusters.
  5. TIMEFRAME — favor swing expiries; deprioritize 0DTE lottos and insane-OTM
     lottery strikes.
  6. CONFLUENCE — agree with structure/regime/discount. Annotate + soft boost only
     (the harness supplies sector RRG call, breadth regime, golden pocket, and the
     volume-profile value area); it never hard-suppresses.

`classify_contract(c)` returns conviction (0–100), a classification bucket, a
signed direction, and a factor breakdown. Hard-gate failures short-circuit to
`noise` with the reason, so the UI/alerts can explain why something was ignored.
"""

import math

# --- Rule 2: size ----------------------------------------------------------
MIN_NOTIONAL          = 250_000   # $ premium floor for "meaningful size"
NOTIONAL_VS_BASELINE  = 3.0       # contract notional ≥ this × the ticker's avg daily options notional
OI_SIZE_MULT          = 1.0       # session volume ≥ this × OI also counts as large-vs-OI (Rule 2 leg)

# --- Rule 3: VOL vs OI -----------------------------------------------------
VOL_OI_CHURN          = 1.0       # below this (VOL < OI) = churn → noise
VOL_OI_INTERESTING    = 5.0       # "interesting"
VOL_OI_SERIOUS        = 20.0      # "this is serious" — likely all fresh money

# --- Rule 5: timeframe -----------------------------------------------------
DTE_MIN_SWING         = 21        # below this = weekly/0DTE lotto territory
DTE_LEAPS             = 270       # at/above this = LEAPS
MAX_OTM_PCT           = 0.30      # |strike/spot − 1| beyond this = insane-OTM lottery ticket

# --- weights (graded conviction, theory/judgment-fixed) --------------------
W_VOLOI      = 34.0   # VOL/OI tier — the strongest single tell of fresh money
W_SIZE       = 26.0   # size in context (notional vs baseline)
W_CLUSTER    = 14.0   # repeated prints / accumulation
W_TIMEFRAME  = 10.0   # swing-friendly expiry
W_AGGRESSOR  = 8.0    # ESTIMATED A/AA — advisory only (small; never a gate on Schwab)
W_CONFLUENCE = 18.0   # structure/regime/discount agreement (soft boost)
CLUSTER_FULL = 4      # cluster_count at which the cluster factor saturates

# --- classification thresholds (0–100) -------------------------------------
T_WATCH      = 35.0
T_NOTABLE    = 55.0   # alerts fire at ≥ this
T_CONVICTION = 72.0

CLASS_ORDER = ("noise", "watch", "notable", "conviction")


def _ok(v):
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def estimate_aggressor(last, bid, ask):
    """Estimate trade aggressor from a snapshot's last vs the bid/ask — the Schwab
    approximation of the trader's A/AA. Returns one of
    {above_ask, ask, mid, bid, below_bid, unknown}. This is a GUESS (the poll's
    `last` isn't necessarily the price every contract in the volume delta traded
    at); a Polygon source replaces it with the confirmed tape side."""
    if not _ok(last):
        return "unknown"
    if _ok(ask) and last >= ask:
        return "above_ask" if last > ask else "ask"
    if _ok(bid) and last <= bid:
        return "below_bid" if last < bid else "bid"
    return "mid"


def expiry_bucket(dte):
    if not _ok(dte):
        return "unknown"
    if dte <= 1:
        return "0dte"
    if dte < DTE_MIN_SWING:
        return "weekly"
    if dte >= DTE_LEAPS:
        return "leaps"
    return "swing"


def _confluence_alignment(direction, confluence):
    """Soft agreement of the flow's direction with the harness's structure signals
    → a value in [-1, +1] (Rule 6: BX≈sector RRG call + breadth regime, structure,
    discount≈golden pocket / volume-profile value area). Annotate + soft boost; it
    is never a gate. `confluence` keys are all optional / fail-soft."""
    if not confluence or direction not in ("bullish", "bearish"):
        return 0.0, []
    want = 1.0 if direction == "bullish" else -1.0
    score, tags = 0.0, []

    call = (confluence.get("sector_call") or "").upper()
    if call in ("ROTATE IN", "HOLD"):
        score += 0.35 * want; tags.append("sector " + call.title())
    elif call in ("ROTATE OUT", "AVOID"):
        score += 0.35 * (-want); tags.append("sector " + call.title())

    regime = (confluence.get("regime") or "").upper()
    if regime == "HEALTHY":
        score += 0.25 * want; tags.append("regime healthy")
    elif regime == "DETERIORATING":
        score += 0.25 * (-want); tags.append("regime deteriorating")

    # discount/premium from the volume-profile value area (and golden pocket):
    # buying calls in discount (below value) agrees with bullish flow; premium agrees
    # with bearish. Disagreement (chasing premium on a call) pulls the score down.
    zone = confluence.get("vp_zone")
    if zone == "discount":
        score += 0.40 * want; tags.append("discount")
    elif zone == "premium":
        score += 0.40 * (-want); tags.append("premium")
    if confluence.get("golden_pocket"):
        score += 0.25 * want; tags.append("golden pocket")

    # accumulation/distribution footprint: net institutional buying (A/B) agrees with
    # bullish flow, net selling (D/E) agrees with bearish. Soft, never a gate.
    rating = (confluence.get("accumulation") or "").upper()
    if rating in ("A", "B"):
        score += 0.30 * want; tags.append("accum " + rating)
    elif rating in ("D", "E"):
        score += 0.30 * (-want); tags.append("distrib " + rating)

    return max(-1.0, min(1.0, score)), tags


def classify_contract(c):
    """Score one contract's flow event. `c` is a normalized dict:
      underlying, option_symbol, put_call ('CALL'/'PUT'), strike, spot, dte,
      session_volume, volume_delta, open_interest, bid, ask, last, mark,
      cluster_count (default 1), baseline_notional (ticker avg daily options $, or
      None), aggressor / aggressor_method (or None → estimate from last/bid/ask),
      confluence (dict or None).

    Returns {conviction, classification, direction, notional, vol_oi_ratio,
    notional_vs_baseline, expiry_bucket, moneyness, aggressor, aggressor_method,
    factors, drop_reason}."""
    pc      = (c.get("put_call") or "").upper()
    spot    = c.get("spot")
    strike  = c.get("strike")
    mark    = c.get("mark") or c.get("last")
    svol    = c.get("session_volume") or 0
    oi      = c.get("open_interest") or 0
    dte     = c.get("dte")
    cluster = int(c.get("cluster_count") or 1)
    base    = c.get("baseline_notional")

    notional = (svol * mark * 100) if _ok(mark) else 0.0
    vol_oi   = (svol / oi) if oi else (float("inf") if svol > 0 else 0.0)
    n_vs_b   = (notional / base) if (base and base > 0) else None
    bucket   = expiry_bucket(dte)
    moneyness = ((strike / spot) - 1.0) if (_ok(spot) and spot and _ok(strike)) else None

    aggressor = c.get("aggressor") or estimate_aggressor(c.get("last"), c.get("bid"), c.get("ask"))
    agg_method = c.get("aggressor_method") or "estimated"

    # CALL buy = bullish, PUT buy = bearish (the dominant opening-buy reading).
    direction = "bullish" if pc == "CALL" else ("bearish" if pc == "PUT" else "unclear")
    conf_align, conf_tags = _confluence_alignment(direction, c.get("confluence"))

    out = {
        "conviction": 0.0, "classification": "noise", "direction": direction,
        "notional": round(notional, 0), "vol_oi_ratio": round(vol_oi, 2) if vol_oi != float("inf") else None,
        "notional_vs_baseline": round(n_vs_b, 2) if n_vs_b is not None else None,
        "expiry_bucket": bucket, "moneyness": round(moneyness, 4) if moneyness is not None else None,
        "aggressor": aggressor, "aggressor_method": agg_method,
        "factors": [], "drop_reason": None,
    }

    # --- Rule 7 hard gates: disqualify noise (confluence can rescue a lotto) ---
    strong_conf = conf_align >= 0.5
    if notional < MIN_NOTIONAL:
        out["drop_reason"] = "size below floor"
        return out
    if vol_oi <= VOL_OI_CHURN:
        out["drop_reason"] = "VOL ≤ OI (churn, not fresh)"
        return out
    if bucket == "0dte" and not strong_conf:
        out["drop_reason"] = "0DTE lotto"
        return out
    if moneyness is not None and abs(moneyness) > MAX_OTM_PCT and not strong_conf:
        out["drop_reason"] = "insane-OTM lottery strike"
        return out

    # --- graded conviction (passed the gates) ---
    factors = []
    score = 0.0

    def add(amt, label):
        nonlocal score
        if abs(amt) < 1e-9:
            return
        score += amt
        factors.append([label, round(amt, 1)])

    # Rule 3 — VOL/OI tier (the headline)
    if vol_oi == float("inf") or vol_oi >= VOL_OI_SERIOUS:
        add(W_VOLOI, f"VOL/OI≥{VOL_OI_SERIOUS:.0f}× (serious)")
    elif vol_oi >= VOL_OI_INTERESTING:
        frac = (vol_oi - VOL_OI_INTERESTING) / (VOL_OI_SERIOUS - VOL_OI_INTERESTING)
        add(W_VOLOI * (0.55 + 0.45 * frac), f"VOL/OI {vol_oi:.0f}× (interesting)")
    else:
        add(W_VOLOI * 0.3, f"VOL/OI {vol_oi:.0f}×")

    # Rule 2 — size in context (notional vs the ticker's own baseline; vs OI)
    if n_vs_b is not None:
        add(W_SIZE * min(1.0, n_vs_b / (NOTIONAL_VS_BASELINE * 2)), f"{n_vs_b:.1f}× ticker avg $")
    elif notional >= MIN_NOTIONAL * 4:
        add(W_SIZE * 0.5, "large absolute $")            # no baseline yet → partial credit
    if oi and svol >= OI_SIZE_MULT * oi:
        add(W_SIZE * 0.25, "size ≥ OI")

    # Rule 4 — clusters (repeated accumulation)
    if cluster > 1:
        add(W_CLUSTER * min(1.0, (cluster - 1) / (CLUSTER_FULL - 1)), f"{cluster} prints (cluster)")

    # Rule 5 — timeframe fit
    tf_q = {"swing": 1.0, "leaps": 0.7, "weekly": 0.4, "0dte": 0.1}.get(bucket, 0.3)
    add(W_TIMEFRAME * tf_q, f"{bucket} expiry")

    # Rule 1 — ESTIMATED aggressor (advisory; small). Bullish flow wants buys at the
    # ask, bearish flow on puts likewise; only reward aggression in the flow's favor.
    if aggressor in ("ask", "above_ask"):
        add(W_AGGRESSOR, f"at/above ask ({agg_method})")
    elif aggressor in ("bid", "below_bid"):
        add(-W_AGGRESSOR * 0.5, f"at/below bid ({agg_method})")

    # Rule 6 — confluence (soft boost, signed by agreement)
    if conf_tags:
        add(W_CONFLUENCE * conf_align, "confluence: " + "+".join(conf_tags))

    score = max(0.0, min(100.0, score))
    if score >= T_CONVICTION:
        cls = "conviction"
    elif score >= T_NOTABLE:
        cls = "notable"
    elif score >= T_WATCH:
        cls = "watch"
    else:
        cls = "noise"

    out["conviction"] = round(score, 1)
    out["classification"] = cls
    out["factors"] = factors
    return out
