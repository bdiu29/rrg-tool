"""
Unit tests for screener snapshot math, against small hand-built panels.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest
from datetime import datetime

import numpy as np
import pandas as pd

from modules.screener import metrics


def _dates(n, start="2026-01-05"):
    return pd.bdate_range(start, periods=n).strftime("%Y-%m-%d")


def _panels(n=70):
    """JUMP: flat at 10 for n-1 days then jumps to 12 on 3× volume with a
    +10% open gap. FLAT: pinned at 10 throughout. STALE: no bars on the
    last two days."""
    idx = _dates(n)
    close = pd.DataFrame({
        "JUMP":  [10.0] * (n - 1) + [12.0],
        "FLAT":  [10.0] * n,
        "STALE": [10.0] * (n - 2) + [np.nan, np.nan],
    }, index=idx)
    open_ = close.copy()
    open_.iloc[-1, open_.columns.get_loc("JUMP")] = 11.0
    high   = close + 0.2
    low    = close - 0.2
    volume = pd.DataFrame({
        "JUMP":  [100_000.0] * (n - 1) + [300_000.0],
        "FLAT":  [100_000.0] * n,
        "STALE": [100_000.0] * (n - 2) + [np.nan, np.nan],
    }, index=idx)
    spy = pd.Series([50.0] * (n - 1) + [51.0], index=idx)
    return close, volume, open_, high, low, spy


class TestComputeSnapshot(unittest.TestCase):
    def setUp(self):
        snap = metrics.compute_snapshot(*_panels())
        self.snap = snap.set_index("symbol")

    def test_change_and_gap(self):
        jump = self.snap.loc["JUMP"]
        self.assertAlmostEqual(jump["chg_pct"], 20.0, places=6)
        self.assertAlmostEqual(jump["gap_pct"], 10.0, places=6)   # open 11 vs prev close 10
        self.assertAlmostEqual(self.snap.loc["FLAT", "chg_pct"], 0.0, places=6)

    def test_volume_fields(self):
        jump = self.snap.loc["JUMP"]
        self.assertAlmostEqual(jump["vol_chg_pct"], 200.0, places=6)
        # rvol divides by the average of the 10 days BEFORE the spike day
        self.assertAlmostEqual(jump["rvol_10d"], 3.0, places=6)
        # stored avg includes the spike day (base for the next live session)
        self.assertAlmostEqual(jump["avg_vol_10d"], 120_000.0, places=2)
        self.assertAlmostEqual(self.snap.loc["FLAT", "rvol_10d"], 1.0, places=6)

    def test_rsi_extremes(self):
        # one up-move after a long flat stretch → avg_down 0 → RSI 100
        self.assertAlmostEqual(self.snap.loc["JUMP", "rsi14"], 100.0, places=6)
        # never any movement → 0/0 → NaN (and NaN never matches a filter)
        self.assertTrue(np.isnan(self.snap.loc["FLAT", "rsi14"]))

    def test_atr_constant_range(self):
        # FLAT's true range is 0.4 every day → Wilder ATR converges to 0.4
        self.assertAlmostEqual(self.snap.loc["FLAT", "atr14"], 0.4, places=6)
        self.assertAlmostEqual(self.snap.loc["FLAT", "atr_pct"], 4.0, places=6)

    def test_levels_are_prior_day_so_cross_is_detectable(self):
        jump = self.snap.loc["JUMP"]
        # yesterday's 20d high of highs is 10.2 — today's 12 crosses it
        self.assertAlmostEqual(jump["high_20d"], 10.2, places=6)
        self.assertGreater(jump["close"], jump["high_20d"])
        self.assertAlmostEqual(jump["high_252"], 10.2, places=6)
        # % off 52w high uses the INCLUSIVE extreme: 12 vs 12.2
        self.assertAlmostEqual(jump["pct_from_52w_high"],
                               (12.0 / 12.2 - 1) * 100, places=6)

    def test_rs_vs_spy(self):
        # JUMP +20% over both windows, SPY +2% → RS = +18
        self.assertAlmostEqual(self.snap.loc["JUMP", "rs_1m_pct"], 18.0, places=6)
        self.assertAlmostEqual(self.snap.loc["JUMP", "rs_3m_pct"], 18.0, places=6)

    def test_min_periods_guards(self):
        # only 70 bars → SMA150/200 must be NaN, SMA20/50 defined
        jump = self.snap.loc["JUMP"]
        self.assertTrue(np.isnan(jump["sma150"]))
        self.assertTrue(np.isnan(jump["sma200"]))
        self.assertFalse(np.isnan(jump["sma20"]))
        self.assertFalse(np.isnan(jump["sma50"]))

    def test_stale_symbol_goes_nan(self):
        stale = self.snap.loc["STALE"]
        self.assertTrue(np.isnan(stale["close"]))      # never matches filters
        self.assertLess(stale["date"], self.snap.loc["JUMP", "date"])


class TestSessionMath(unittest.TestCase):
    def test_session_fraction(self):
        f = metrics.session_fraction
        self.assertAlmostEqual(f(datetime(2026, 6, 11, 9, 30)), 0.02)    # floor
        self.assertAlmostEqual(f(datetime(2026, 6, 11, 12, 45)), 0.5)
        self.assertAlmostEqual(f(datetime(2026, 6, 11, 16, 0)), 1.0)
        self.assertAlmostEqual(f(datetime(2026, 6, 11, 17, 30)), 1.0)    # clamp

    def test_live_rvol(self):
        # 600k traded by midday vs a 400k full-day average → 3× pace
        self.assertAlmostEqual(metrics.live_rvol(600_000, 400_000, 0.5), 3.0)
        self.assertIsNone(metrics.live_rvol(600_000, None, 0.5))
        self.assertIsNone(metrics.live_rvol(600_000, 0, 0.5))
        self.assertIsNone(metrics.live_rvol(None, 400_000, 0.5))


if __name__ == "__main__":
    unittest.main()
