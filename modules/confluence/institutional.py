"""
Institutional sponsorship — CANSLIM's **I**, a pure leaf (no I/O). Unlike the
price/volume leaves here, this one reads **ownership** numbers (institutional % held
+ holder count, from 13F-derived data the consumer fetches), so it takes those
numbers as inputs rather than OHLCV — the leaf stays pure; the fetch + the quarter-
over-quarter history live in the consumer (the screener fundamentals store).

O'Neil wants **increasing** quality sponsorship — a few funds are fine, a rising
number is the tell — but **not over-ownership** (a name that's ~fully institutionally
held has no marginal buyer left). So the signal is mostly the **QoQ change**, with a
small level component:

  * rising % held / rising holder count  → accumulation by funds → bullish
  * a healthy sponsorship band            → mild positive (backing without crowding)
  * lacking sponsorship (< LOW)           → mild negative (no institutional backing)
  * crowded (> CROWDED)                   → mild negative (no room / late)

Consumed by the **CANSLIM** module (the I letter); NOT wired into RRG sector conviction
(institutional ownership is meaningless for ETFs). Available to the flow tool if wanted.

Caveat (the consumer surfaces it): 13F/yfinance ownership is a quarterly snapshot, so
the QoQ delta is **forward-accumulating** — it emerges only once the store has two
dated reads; before then `current()` degrades to the level-only read.
"""

import math

NAME = "institutional"

# thresholds (judgment/theory-fixed) -----------------------------------------
LOW_SPONSORSHIP = 0.15    # % held below this = lacking institutional backing
CROWDED         = 0.92    # % held above this = over-owned / no marginal buyer
DELTA_STRONG    = 0.05    # a QoQ Δ%-held of this magnitude = a strong move
W_LEVEL  = 0.2            # weight of the level (band) read
W_DELTA  = 0.6           # weight of the QoQ %-held change (the real signal)
W_HOLDERS = 0.2          # weight of the holder-count direction


def _isnum(v):
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def current(pct_held, pct_held_prev=None, holders=None, holders_prev=None):
    """Last-read institutional sponsorship for one symbol. `pct_held` is a fraction
    (0.62 = 62% held). `*_prev` are the prior-quarter reads (None on a first read →
    level-only). Returns a dict or None when there's no current %-held."""
    if not _isnum(pct_held):
        return None
    delta = (pct_held - pct_held_prev) if _isnum(pct_held_prev) else None
    holders_delta = (holders - holders_prev) if (_isnum(holders) and _isnum(holders_prev)) else None
    if delta is None:
        trend = "unknown"
    elif delta > 0:
        trend = "accumulating"
    elif delta < 0:
        trend = "distributing"
    else:
        trend = "flat"
    return {
        "pct_held":      round(float(pct_held), 4),
        "pct_held_prev": round(float(pct_held_prev), 4) if _isnum(pct_held_prev) else None,
        "delta":         round(float(delta), 4) if delta is not None else None,
        "holders":       int(holders) if _isnum(holders) else None,
        "holders_delta": int(holders_delta) if holders_delta is not None else None,
        "trend":         trend,
    }


def contribution(read):
    """Graded signed sponsorship strength in [-1, +1] (+rising/healthy / −lacking or
    crowded or distributing) → (strength, label). The consumer scales/maps it."""
    if not read:
        return 0.0, ""
    p = read.get("pct_held")
    if not _isnum(p):
        return 0.0, ""
    s = 0.0
    # level band
    if p < LOW_SPONSORSHIP:
        s -= W_LEVEL * 1.5            # no institutional backing is a real negative
    elif p > CROWDED:
        s -= W_LEVEL
    else:
        s += W_LEVEL
    # QoQ %-held change — the dominant signal when available
    d = read.get("delta")
    if d is not None:
        s += max(-1.0, min(1.0, d / DELTA_STRONG)) * W_DELTA
    # holder-count direction
    hd = read.get("holders_delta")
    if hd is not None:
        s += W_HOLDERS * (1.0 if hd > 0 else -1.0 if hd < 0 else 0.0)
    s = max(-1.0, min(1.0, s))
    if abs(s) <= 1e-9:
        return 0.0, ""
    trend = read.get("trend", "unknown")
    pct = f"{p * 100:.0f}% held"
    tag = f", {trend}" if trend in ("accumulating", "distributing") else ""
    return s, f"inst {pct}{tag}"


def score(read):
    """CANSLIM I letter, 0-100 (the signed contribution mapped onto a 0-100 scale).
    None when there's no read."""
    if not read:
        return None
    s, _ = contribution(read)
    return round(50.0 + 50.0 * s, 1)
