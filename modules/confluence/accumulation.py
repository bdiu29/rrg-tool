"""
Accumulation / distribution — institutional buying-vs-selling footprint, a pure
leaf (numpy/pandas, no I/O). Read on the symbol's own ABSOLUTE price+volume (like
`exhaustion` / `volume_profile`; the RS line the wave engine works on carries no
volume).

This is the "big money" demand signal that `volume_profile` (price *location*) and
`exhaustion` (a one-bar *climax*) don't measure — the standing read of whether the
trailing window was net ACCUMULATED (bought) or DISTRIBUTED (sold). It serves O'Neil
/ IBD's **S** (supply & demand → demand side) and the price-volume read of **I**
(institutional sponsorship). Three independent tells, blended:

  * **U/D Volume Ratio** — Σ volume on up-close days ÷ Σ on down-close days over the
    trailing window (IBD's "Up/Down Volume Ratio"). >1 accumulation, <1 distribution.
  * **Chaikin A/D line** — `mfm=((close-low)-(high-close))/(high-low)`, `adl=cumsum(mfm·vol)`;
    its direction vs the price's over the window gives a **divergence** read
    (`distribution` = price up while A/D falls — the classic "selling into strength";
    `accumulation` = price down while A/D rises; else `confirming`).
  * **Accumulation / distribution days** — big up/down days (close in the top/bottom
    half of the bar's range on ≥`BIG_DAY_VOL`× avg volume — the institutional footprint).

Blended into a signed strength in [-1, +1] (+accum / −distrib) and an A–E **rating**
(A = strong accumulation … E = heavy distribution). No-lookahead: the volume baseline
is trailing (`shift(1)`) and the read at the last bar uses only completed bars.
`panels` vectorizes the rating for the screener; `current` is the last-bar read for
the RRG conviction factor / flow Rule-6. The consumer scales `contribution` by a
theory-fixed weight (rrg `W_ACCUM`).
"""

import numpy as np
import pandas as pd

NAME = "accumulation"

# --- parameters (single source of truth) -----------------------------------
WINDOW       = 50      # trailing bars for the U/D ratio + A/D slope (IBD's ~50d)
VOL_AVG      = 20      # trailing window for the average-volume baseline (big days)
BIG_DAY_VOL  = 1.25    # volume ≥ this × the trailing average = an institutional day
UD_CAP       = 10.0    # cap the U/D ratio (no down-volume → strong, not infinite)

# U/D-ratio → strength thresholds (centred on 1.0 = balanced)
UD_STRONG = 1.5
UD_MILD   = 1.1
UD_SOFT   = 0.9
UD_WEAK   = 0.6

# strength blend weights (theory-fixed; the U/D ratio is the spine)
W_ADL    = 0.2         # A/D-line direction (+1 rising / −1 falling)
W_DAYS   = 0.3         # accum-vs-distrib day balance (in [-1, 1])
W_DIVERG = 0.3         # divergence amplifier (distribution/accumulation)


def _strength(ud, adl_dir, day_balance, divergence):
    """Signed accumulation strength in [-1, +1] (+accum / −distrib). All args
    broadcastable (scalars or numpy arrays) so `current` and `panels` share one
    scoring policy. `divergence` is the string/str-array tell."""
    ud = np.asarray(ud, dtype=float)
    s = np.select(
        [ud >= UD_STRONG, ud >= UD_MILD, ud <= UD_WEAK, ud <= UD_SOFT],
        [0.5, 0.25, -0.5, -0.25], default=0.0)
    s = s + W_ADL * np.asarray(adl_dir, dtype=float)
    s = s + W_DAYS * np.asarray(day_balance, dtype=float)
    div = np.asarray(divergence)
    s = s + np.where(div == "distribution", -W_DIVERG, 0.0)
    s = s + np.where(div == "accumulation",  W_DIVERG, 0.0)
    return np.clip(s, -1.0, 1.0)


def _rating(strength):
    """Strength → IBD-style A–E letter (A strong accumulation … E heavy distribution).
    Works on a scalar or an array."""
    s = np.asarray(strength, dtype=float)
    return np.select([s >= 0.5, s >= 0.2, s <= -0.5, s <= -0.2],
                     ["A", "B", "E", "D"], default="C")


def panels(high, low, close, volume, window=WINDOW):
    """date×symbol panel of the A–E accumulation rating (NaN where the trailing
    window isn't fully formed). The vectorized twin of `current` — same scoring."""
    prev = close.shift(1)
    up, down = close > prev, close < prev

    up_vol   = volume.where(up,   0.0).rolling(window, min_periods=window).sum()
    down_vol = volume.where(down, 0.0).rolling(window, min_periods=window).sum()
    ud = (up_vol / down_vol).replace([np.inf, -np.inf], UD_CAP).clip(upper=UD_CAP)
    ud = ud.where(~(up_vol.notna() & ud.isna()), 1.0)   # 0/0 degenerate → neutral

    rng = (high - low).replace(0, np.nan)
    mfm = (((close - low) - (high - close)) / rng).fillna(0.0)
    adl = (mfm * volume).cumsum()
    adl_dir = np.sign(adl - adl.shift(window))

    avg_vol = volume.rolling(VOL_AVG, min_periods=VOL_AVG).mean().shift(1)
    pos = (close - low) / rng
    big = volume >= BIG_DAY_VOL * avg_vol
    accum_n   = (big & up   & (pos >= 0.5)).rolling(window, min_periods=window).sum()
    distrib_n = (big & down & (pos <= 0.5)).rolling(window, min_periods=window).sum()
    tot = accum_n + distrib_n
    day_balance = ((accum_n - distrib_n) / tot.replace(0, np.nan)).fillna(0.0)

    price_dir = np.sign(close - close.shift(window))
    diverg = pd.DataFrame("confirming", index=close.index, columns=close.columns, dtype=object)
    diverg = diverg.where(~((price_dir > 0) & (adl_dir < 0)), "distribution")
    diverg = diverg.where(~((price_dir < 0) & (adl_dir > 0)), "accumulation")

    strength = _strength(ud.to_numpy(), adl_dir.to_numpy(),
                         day_balance.to_numpy(), diverg.to_numpy())
    out = pd.DataFrame(_rating(strength), index=close.index,
                       columns=close.columns, dtype=object)
    valid = ud.notna() & close.notna() & avg_vol.notna()
    return out.where(valid)


def current(high, low, close, volume, window=WINDOW):
    """Last-bar accumulation read for one symbol (Series in). Returns a dict
    {ud_ratio, rating, adl_dir, divergence, accum_days, distrib_days, n_bars} or
    None when there aren't enough bars to read the trailing window."""
    close = pd.Series(close).dropna()
    if len(close) < window + VOL_AVG:
        return None
    idx  = close.index
    high = pd.Series(high).reindex(idx)
    low  = pd.Series(low).reindex(idx)
    vol  = pd.Series(volume).reindex(idx)

    prev = close.shift(1)
    up, down = close > prev, close < prev
    seg = slice(-window, None)
    up_vol   = float(vol.iloc[seg][up.iloc[seg]].sum())
    down_vol = float(vol.iloc[seg][down.iloc[seg]].sum())
    if down_vol > 0:
        ud = min(up_vol / down_vol, UD_CAP)
    else:
        ud = UD_CAP if up_vol > 0 else 1.0

    rng = (high - low).replace(0, np.nan)
    mfm = (((close - low) - (high - close)) / rng).fillna(0.0)
    adl = (mfm * vol).cumsum()
    adl_dir = float(np.sign(adl.iloc[-1] - adl.iloc[-window]))

    avg_vol = vol.rolling(VOL_AVG, min_periods=VOL_AVG).mean().shift(1)
    pos = (close - low) / rng
    big = vol >= BIG_DAY_VOL * avg_vol
    accum_n   = int((big & up   & (pos >= 0.5)).iloc[seg].sum())
    distrib_n = int((big & down & (pos <= 0.5)).iloc[seg].sum())
    tot = accum_n + distrib_n
    day_balance = (accum_n - distrib_n) / tot if tot else 0.0

    price_dir = float(np.sign(close.iloc[-1] - close.iloc[-window]))
    divergence = "confirming"
    if price_dir > 0 and adl_dir < 0:
        divergence = "distribution"
    elif price_dir < 0 and adl_dir > 0:
        divergence = "accumulation"

    strength = float(_strength(ud, adl_dir, day_balance, divergence))
    return {
        "ud_ratio":     round(ud, 3),
        "rating":       str(_rating(strength)),
        "adl_dir":      adl_dir,
        "divergence":   divergence,
        "accum_days":   accum_n,
        "distrib_days": distrib_n,
        "n_bars":       int(min(window, len(close))),
    }


def contribution(read):
    """Graded signed conviction contribution from a `current` read → (strength, label),
    strength in [-1, +1] (+accum / −distrib); the consumer scales by W_ACCUM. Recomputed
    from the read's facts (the scoring policy lives here, like volume_profile)."""
    if not read:
        return 0.0, ""
    strength = float(_strength(read.get("ud_ratio", 1.0), read.get("adl_dir", 0.0),
                               _day_balance(read), read.get("divergence", "confirming")))
    if abs(strength) <= 1e-9:
        return 0.0, ""
    rating = read.get("rating", "C")
    div = read.get("divergence")
    tag = f" {div}" if div in ("distribution", "accumulation") else ""
    return strength, f"A/D {rating} (U/D {read.get('ud_ratio', 0):.2f}){tag}"


def _day_balance(read):
    a, d = read.get("accum_days", 0), read.get("distrib_days", 0)
    tot = a + d
    return (a - d) / tot if tot else 0.0
