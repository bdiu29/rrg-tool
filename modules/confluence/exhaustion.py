"""
Volume buyer/seller exhaustion — pure pandas, no I/O, no signal import.

A *volume climax* into a fresh extreme that closes back against the move signals
that one side is exhausted — a topping or bottoming tell that adds confluence to
the Elliott-wave analysis (where the RS line itself carries no volume, so this is
read on the symbol's own absolute price + volume):

  * **buyer exhaustion** (topping): a new EXT_LOOKBACK-bar HIGH on a volume spike
    that closes in the LOWER part of its range (an upthrust / blow-off — buyers
    spent into supply).
  * **seller exhaustion** (bottoming): a new low on a volume spike that closes in
    the UPPER part of its range (a selling climax / capitulation reversal).

No-lookahead: the volume baseline and rolling extreme are trailing (`shift(1)`),
so every read uses only completed prior bars. `exhaustion_panels` vectorizes it
for the screener; `current_exhaustion` is the last-row scalar for the RRG factor.
"""

import numpy as np
import pandas as pd

VOL_AVG        = 20      # trailing window for the average-volume baseline
VOL_CLIMAX_MULT = 2.0    # volume ≥ this × the trailing average = a climax spike
EXT_LOOKBACK   = 20      # bars defining a "new high / low"
WEAK_CLOSE     = 0.4     # close in the bottom 40% of the bar's range → weak (topping)
STRONG_CLOSE   = 0.6     # close in the top 40% (≥0.6) of the range → strong (bottoming)


def exhaustion_panels(high, low, close, volume):
    """date×symbol panel of "buyer"/"seller"/"none" (NaN where inputs are NaN)."""
    avg_vol = volume.rolling(VOL_AVG, min_periods=VOL_AVG).mean().shift(1)
    vspike  = volume >= VOL_CLIMAX_MULT * avg_vol

    rng = (high - low).replace(0, np.nan)
    pos = (close - low) / rng               # 0 = close at low, 1 = close at high

    at_high = high >= high.rolling(EXT_LOOKBACK, min_periods=EXT_LOOKBACK).max().shift(1)
    at_low  = low  <= low.rolling(EXT_LOOKBACK, min_periods=EXT_LOOKBACK).min().shift(1)

    buyer  = vspike & at_high & (pos <= WEAK_CLOSE)
    seller = vspike & at_low  & (pos >= STRONG_CLOSE)

    out = pd.DataFrame("none", index=close.index, columns=close.columns, dtype=object)
    out = out.where(~buyer.fillna(False), "buyer")
    out = out.where(~seller.fillna(False), "seller")
    # mask windows that aren't fully formed (NaN inputs) back to NaN, not "none"
    valid = close.notna() & volume.notna() & avg_vol.notna() & rng.notna()
    return out.where(valid)


def current_exhaustion(high, low, close, volume):
    """Last-bar exhaustion read for one symbol (Series in) → "buyer"/"seller"/None."""
    panel = exhaustion_panels(high.to_frame("x"), low.to_frame("x"),
                              close.to_frame("x"), volume.to_frame("x"))
    if panel.empty:
        return None
    val = panel["x"].iloc[-1]
    if val in ("buyer", "seller"):
        return val
    return None
