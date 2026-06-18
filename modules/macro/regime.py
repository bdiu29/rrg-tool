"""
The macro-regime engine — a deterministic growth×inflation 4-quadrant classifier
with PROBABILITIES, not a single label.

This is the strategic backdrop read (orthogonal to the breadth HEALTHY/NEUTRAL/
DETERIORATING *tactical* regime): where are we in the growth/inflation cycle, and
how confident is that read? It is pure math (no I/O, no LLM) so it is replayable /
backtestable and can feed the autonomous-trading decision the same way the
combiner does — the LLM only INTERPRETS it on the page.

Two axes, each a self-normalized z-score from `indicators.regime_features`:
  growth_z    — copper/gold, credit (tight), cyclicals>defensives, small caps,
                falling claims, breadth participation
  inflation_z — 10Y breakeven, copper, energy leadership, gold

Map each axis through a sigmoid → P(growth up) and P(inflation up), then the four
quadrant probabilities are the products (they sum to 1 by construction):

  Goldilocks   = p_g·(1-p_i)   growth↑ inflation↓  — best tape for stocks
  Reflation    = p_g·p_i       growth↑ inflation↑  — cyclicals / real assets
  Stagflation  = (1-p_g)·p_i   growth↓ inflation↑  — defend capital
  Disinflation = (1-p_g)·(1-p_i) growth↓ inflation↓ — quality / duration / defensives
"""

import math

# Sensitivity of each axis sigmoid. ~0.9 spreads typical z-scores (-2..2) into a
# 0.14..0.86 probability — confident but never certain (the shift-risk read stays live).
REGIME_K = 0.9

# Per-quadrant metadata: the plain-english playbook + an equity tilt the harness
# vote reads (+ = supportive of equity risk, − = defensive).
_META = {
    "Goldilocks": {
        "axes": "growth expanding, inflation cooling",
        "tilt": 1.0,
        "playbook": ("Disinflationary growth — historically the best backdrop for stocks. "
                     "Favor growth and momentum (tech, semiconductors, quality compounders); "
                     "let winners run and keep some dry powder for dips."),
    },
    "Reflation": {
        "axes": "growth and inflation both rising",
        "tilt": 0.55,
        "playbook": ("Growth with rising prices — favor cyclicals and real assets: energy, "
                     "materials, industrials and financials, plus the value end of the market. "
                     "Trim long-duration growth if yields keep climbing."),
    },
    "Stagflation": {
        "axes": "growth slowing while inflation stays hot",
        "tilt": -0.85,
        "playbook": ("Slowing growth with sticky inflation — the toughest tape. Lean on real "
                     "assets, energy and commodities, raise cash, and keep position sizes small. "
                     "Defense over offense."),
    },
    "Disinflation": {
        "axes": "growth and inflation both cooling",
        "tilt": -0.3,
        "playbook": ("Cooling growth and cooling prices — late-cycle. Favor quality and "
                     "defensives (staples, utilities, healthcare) and let duration (bonds) work; "
                     "pare back cyclical and small-cap risk."),
    },
}

ORDER = ["Goldilocks", "Reflation", "Stagflation", "Disinflation"]


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def _shift_risk(margin):
    """How fragile the top read is, from the top-two probability margin."""
    if margin >= 0.18:
        return "Low"
    if margin >= 0.07:
        return "Moderate"
    return "High"


def _driver(features, top_regime):
    """The 'Driver:' line — the strongest feature inputs pushing the current read."""
    inputs = list(features.get("growth_inputs", [])) + list(features.get("inflation_inputs", []))
    inputs = [(lbl, z) for lbl, z in inputs if z is not None]
    inputs.sort(key=lambda lz: abs(lz[1]), reverse=True)
    names = [lbl for lbl, _ in inputs[:3]]
    return " · ".join(names) if names else _META[top_regime]["axes"]


def classify(features):
    """features: {growth_z, inflation_z, growth_inputs, inflation_inputs}.
    Returns the full regime read (probabilities, confidence, shift risk, driver,
    playbook, equity tilt). Pure — never raises on well-formed numeric input."""
    g = float(features.get("growth_z") or 0.0)
    i = float(features.get("inflation_z") or 0.0)
    p_g, p_i = _sigmoid(REGIME_K * g), _sigmoid(REGIME_K * i)

    probs = {
        "Goldilocks":   p_g * (1 - p_i),
        "Reflation":    p_g * p_i,
        "Stagflation":  (1 - p_g) * p_i,
        "Disinflation": (1 - p_g) * (1 - p_i),
    }
    ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    top, top_p = ranked[0]
    second_p = ranked[1][1]
    margin = top_p - second_p

    # Probability-weighted equity tilt across all four quadrants (a smoother read
    # than the top label alone — what the harness vote consumes).
    tilt = sum(probs[name] * _META[name]["tilt"] for name in probs)

    return {
        "regime":      top,
        "axes":        _META[top]["axes"],
        "confidence":  round(top_p * 100),
        "shift_risk":  _shift_risk(margin),
        "shift_margin": round(margin * 100, 1),
        "growth_dir":   "expanding" if g >= 0 else "slowing",
        "inflation_dir": "rising" if i >= 0 else "cooling",
        "growth_z":     round(g, 2),
        "inflation_z":  round(i, 2),
        "p_growth_up":  round(p_g * 100),
        "p_inflation_up": round(p_i * 100),
        "probabilities": [{"name": n, "prob": round(probs[n] * 100)} for n, _ in ranked],
        "driver":       _driver(features, top),
        "playbook":     _META[top]["playbook"],
        "equity_tilt":  round(tilt, 3),
    }
