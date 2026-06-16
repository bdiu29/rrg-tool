"""
Volume profile — volume-at-price structure, a pure leaf (numpy/pandas, no I/O).

Read on the symbol's own ABSOLUTE price+volume (like `exhaustion`; the RS line the
wave engine works on carries no volume). Buckets traded volume into price bins over
a trailing window, then derives the structure traders watch:

  * POC  (Point of Control) — the highest-volume price; the fairest price / magnet.
  * Value Area (VAH/VAL)    — the band holding ~70% of volume around the POC; a
    "fair value band": price below VAL = discount, above VAH = premium.
  * HVN / LVN               — high/low-volume nodes (acceptance magnets / rejection gaps).
  * Profile SHAPE           — D (balanced) / P (fat top) / b (fat bottom) /
    B (double distribution) / trend (flat, no dominant POC).

Approximation: with only OHLCV bars (no tick tape), each bar's volume is spread
across its high–low range in proportion to bin overlap — the standard bar-based
profile (what TradingView does when fed bars). Finer bars → closer to a true tick
profile. No-lookahead: a read at the last bar uses only the trailing `window` bars.

The factor's conviction CONTRIBUTION (`contribution`) is graded in [-1, +1]; the
consumer (rrg `_conviction`) scales it by the theory-fixed `W_VOL_PROFILE` weight.
"""

import numpy as np
import pandas as pd

NAME = "volume_profile"

# --- parameters (single source of truth) -----------------------------------
BINS          = 50      # price bins in the histogram
WINDOW        = 60      # trailing bars for the composite profile (~3 months daily)
VALUE_AREA    = 0.70    # fraction of volume inside the value area
FLAT_PEAK     = 2.0     # max-bin / mean-bin below this → no dominant POC → "trend"
SIG_PEAK_FRAC = 0.50    # a peak counts as significant at ≥ this × the tallest peak
VALLEY_FRAC   = 0.55    # valley between two peaks ≤ this × the smaller peak → bimodal "B"
POC_HI        = 0.60    # POC in the upper part of the range → P (fat top)
POC_LO        = 0.40    # POC in the lower part → b (fat bottom)
NEAR_NODE_PCT = 0.01    # price within this fraction of a node's price → "near" it


def build_profile(high, low, close, volume, bins=BINS, price_lo=None, price_hi=None):
    """Volume-at-price histogram over the given bars. Each bar's volume is spread
    across [low, high] in proportion to each bin's overlap with that span (a zero-
    range bar drops all its volume in the bin holding its price).

    Returns (centers, vol_bins, lo, hi) — bin centers, volume per bin, and the
    price range — or (None, None, None, None) when there's nothing to bucket."""
    high = np.asarray(high, dtype=float)
    low  = np.asarray(low,  dtype=float)
    vol  = np.asarray(volume, dtype=float)
    ok   = np.isfinite(high) & np.isfinite(low) & np.isfinite(vol) & (vol > 0)
    if not ok.any():
        return None, None, None, None
    high, low, vol = high[ok], low[ok], vol[ok]

    lo = float(np.min(low))  if price_lo is None else price_lo
    hi = float(np.max(high)) if price_hi is None else price_hi
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return None, None, None, None

    edges   = np.linspace(lo, hi, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    vb      = np.zeros(bins)
    left_e, right_e = edges[:-1], edges[1:]
    for h_i, l_i, v_i in zip(high, low, vol):
        span = h_i - l_i
        if span <= 0:                                   # zero-range bar → single bin
            k = int(np.clip(np.searchsorted(edges, l_i) - 1, 0, bins - 1))
            vb[k] += v_i
            continue
        overlap = np.clip(np.minimum(right_e, h_i) - np.maximum(left_e, l_i), 0.0, None)
        s = overlap.sum()
        if s > 0:
            vb += v_i * overlap / s                     # distribute proportional to overlap
    return centers, vb, lo, hi


def value_area(centers, vol_bins, coverage=VALUE_AREA):
    """POC + value-area bounds. Expands outward from the POC bin, taking the larger
    adjacent bin each step until `coverage` of total volume is enclosed.
    Returns (poc_price, val_price, vah_price, poc_idx, lo_idx, hi_idx)."""
    vb = np.asarray(vol_bins, dtype=float)
    total = vb.sum()
    poc = int(np.argmax(vb))
    if total <= 0:
        return centers[poc], centers[poc], centers[poc], poc, poc, poc
    target = coverage * total
    lo_i = hi_i = poc
    cum = vb[poc]
    n = len(vb)
    while cum < target and (lo_i > 0 or hi_i < n - 1):
        below = vb[lo_i - 1] if lo_i > 0 else -1.0
        above = vb[hi_i + 1] if hi_i < n - 1 else -1.0
        if above >= below:
            hi_i += 1; cum += vb[hi_i]
        else:
            lo_i -= 1; cum += vb[lo_i]
    return centers[poc], centers[lo_i], centers[hi_i], poc, lo_i, hi_i


def _peaks(vb):
    """Indices of local maxima (strict left, non-strict right — tolerates a flat top)."""
    return [i for i in range(1, len(vb) - 1) if vb[i] > vb[i - 1] and vb[i] >= vb[i + 1]]


def _is_bimodal(vb):
    """Two significant peaks separated by a deep valley → double distribution."""
    mx = float(np.max(vb)) if len(vb) else 0.0
    if mx <= 0:
        return False
    sig = [i for i in _peaks(vb) if vb[i] >= SIG_PEAK_FRAC * mx]
    for a, b in [(sig[i], sig[j]) for i in range(len(sig)) for j in range(i + 1, len(sig))]:
        if b > a + 1 and float(np.min(vb[a + 1:b])) <= VALLEY_FRAC * min(vb[a], vb[b]):
            return True
    return False


def _skew(centers, vb):
    """Volume-weighted skewness of the price distribution (sign only matters):
    negative = long lower tail (P), positive = long upper tail (b)."""
    w = np.asarray(vb, dtype=float)
    s = w.sum()
    if s <= 0:
        return 0.0
    mean = np.average(centers, weights=w)
    var  = np.average((centers - mean) ** 2, weights=w)
    if var <= 0:
        return 0.0
    return float(np.average((centers - mean) ** 3, weights=w) / var ** 1.5)


def classify_shape(centers, vol_bins):
    """Profile letter shape from the histogram → 'D' / 'P' / 'b' / 'B' / 'trend'.
    Order: bimodal (B) → flat/elongated (trend) → POC position (P high / b low) → D."""
    vb = np.asarray(vol_bins, dtype=float)
    total = vb.sum()
    n = len(vb)
    if total <= 0 or n < 3:
        return "D"
    if _is_bimodal(vb):
        return "B"
    mean = total / np.count_nonzero(vb) if np.count_nonzero(vb) else total / n
    if mean <= 0 or float(np.max(vb)) / mean < FLAT_PEAK:   # no dominant POC
        return "trend"
    poc_pct = int(np.argmax(vb)) / (n - 1)
    if poc_pct >= POC_HI:
        return "P"
    if poc_pct <= POC_LO:
        return "b"
    return "D"


def _nodes(centers, vb, poc_idx):
    """HVN/LVN prices: significant peaks (excl. the POC) and the valleys between."""
    mx = float(np.max(vb)) if len(vb) else 0.0
    hvn = [float(centers[i]) for i in _peaks(vb) if i != poc_idx and vb[i] >= SIG_PEAK_FRAC * mx]
    troughs = [i for i in range(1, len(vb) - 1) if vb[i] < vb[i - 1] and vb[i] <= vb[i + 1]]
    lvn = [float(centers[i]) for i in troughs]
    return hvn, lvn


def current(high, low, close, volume, bins=BINS, window=WINDOW):
    """Last-bar volume-profile read for one symbol (Series in). Builds the profile
    over the trailing `window` bars and locates the latest price within it.

    Returns a dict {shape, poc, vah, val, zone, poc_dist_pct, near_lvn, near_hvn,
    n_bars} or None when there isn't enough volume to build a profile.
    `zone` ∈ {discount (< VAL), value, premium (> VAH)}."""
    close = pd.Series(close).dropna()
    if close.empty:
        return None
    idx = close.index
    high = pd.Series(high).reindex(idx).to_numpy(dtype=float)
    low  = pd.Series(low).reindex(idx).to_numpy(dtype=float)
    vol  = pd.Series(volume).reindex(idx).to_numpy(dtype=float)
    px   = float(close.iloc[-1])

    h, l, v = high[-window:], low[-window:], vol[-window:]
    centers, vb, lo, hi = build_profile(h, l, close.to_numpy()[-window:], v, bins=bins)
    if centers is None:
        return None

    poc, val, vah, poc_i, _lo_i, _hi_i = value_area(centers, vb)
    shape = classify_shape(centers, vb)
    hvn, lvn = _nodes(centers, vb, poc_i)

    zone = "value"
    if px < val:
        zone = "discount"
    elif px > vah:
        zone = "premium"

    def _near(nodes):
        return bool(nodes) and any(abs(px - p) <= NEAR_NODE_PCT * px for p in nodes)

    return {
        "shape":        shape,
        "poc":          round(float(poc), 4),
        "vah":          round(float(vah), 4),
        "val":          round(float(val), 4),
        "zone":         zone,
        "poc_dist_pct": round((px - float(poc)) / float(poc) * 100, 2) if poc else None,
        "near_lvn":     _near(lvn),
        "near_hvn":     _near(hvn + [poc]),
        "n_bars":       int(min(window, len(close))),
    }


def contribution(read, *, price=None):
    """Graded signed conviction contribution from a `current` read → (strength, label),
    strength in [-1, +1] (+bull / −bear); the consumer scales by W_VOL_PROFILE.

    Bullish when price sits in DISCOUNT (below value) — a value buy — reinforced by a
    bottoming **b** shape; bearish in PREMIUM, reinforced by a topping **P** shape.
    Inside value it's near-neutral. Returns (0.0, '') when there's no read / no edge."""
    if not read:
        return 0.0, ""
    zone, shape = read.get("zone"), read.get("shape")
    strength = 0.0
    if zone == "discount":
        strength += 0.6
    elif zone == "premium":
        strength -= 0.6
    if shape == "b":
        strength += 0.3
    elif shape == "P":
        strength -= 0.3
    strength = max(-1.0, min(1.0, strength))
    if abs(strength) <= 1e-9:
        return 0.0, ""
    side = "discount" if strength > 0 else "premium"
    tag  = f"/{shape}" if shape in ("b", "P") else ""
    return strength, f"VP {side}{tag}"
