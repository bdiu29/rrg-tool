"""
Unit tests for the flag detection core (flags.py), volume exhaustion
(exhaustion.py), and the empirical + regime-aware flag/exhaustion factors in the
conviction engine (signal.py).

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest

import numpy as np
import pandas as pd

from modules.confluence import flags, exhaustion
from modules.rrg import signal


def _bull_flag_close():
    """A clean +10% flagpole, a shallow 5-bar flag, then a continued advance —
    one detectable bull flag whose +SUCCESS_H forward return is positive."""
    c = np.concatenate([
        np.linspace(100, 112, 11),                 # pole up ~+12%
        np.array([111, 110.5, 111, 110.8, 111]),   # shallow tight flag
        np.linspace(111, 125, 20),                 # continuation up
    ])
    v = np.full(len(c), 1000.0)
    v[:11] = 2000.0                                 # volume taper into the flag
    return c, v


class TestWinRates(unittest.TestCase):
    def test_bull_flag_detected_and_wins(self):
        c, v = _bull_flag_close()
        wr = flags.win_rates(c, v)
        self.assertGreaterEqual(wr["bull_n"], 1)
        self.assertEqual(wr["bull"], 1.0)          # the continuation succeeded
        self.assertEqual(wr["bear_n"], 0)

    def test_regime_conditioning_keeps_aligned_drops_opposing(self):
        c, v = _bull_flag_close()
        healthy = np.array(["HEALTHY"] * len(c))
        deter   = np.array(["DETERIORATING"] * len(c))
        # bull flags count OUTSIDE deteriorating regimes
        self.assertGreaterEqual(flags.win_rates(c, v, regime_labels=healthy)["bull_n"], 1)
        dropped = flags.win_rates(c, v, regime_labels=deter)
        self.assertEqual(dropped["bull_n"], 0)
        self.assertIsNone(dropped["bull"])

    def test_regime_ok_table(self):
        self.assertTrue(flags._regime_ok("bull", "HEALTHY"))
        self.assertTrue(flags._regime_ok("bull", "NEUTRAL"))
        self.assertFalse(flags._regime_ok("bull", "DETERIORATING"))
        self.assertTrue(flags._regime_ok("bear", "DETERIORATING"))
        self.assertFalse(flags._regime_ok("bear", "HEALTHY"))
        self.assertTrue(flags._regime_ok("bear", None))   # unknown never filters


class TestFlagPanels(unittest.TestCase):
    def test_panel_never_misses_a_scalar_flag(self):
        """The vectorized panel must flag every bar the scalar `_detect` does
        (the panel may add extras — it has no cooldown, by design)."""
        rng = np.random.default_rng(7)
        n = 220
        c = 100 * np.cumprod(1 + rng.normal(0, 0.008, n))
        c[90:100] = c[89] * np.linspace(1.0, 1.10, 10)               # a pole
        c[100:105] = c[99] * np.array([0.99, 0.985, 0.99, 0.988, 0.99])
        close = pd.Series(c)
        vol = pd.Series(rng.uniform(900, 1100, n)); vol[90:100] = 3000
        ev = flags._detect(close.to_numpy(), vol.to_numpy(), require_taper=True)
        scalar = {e[1] for e in ev}
        panel = flags.flag_panels(close.to_frame("X"), vol.to_frame("X"))["X"]
        vector = {i for i, val in enumerate(panel) if val in ("bull", "bear")}
        self.assertTrue(scalar.issubset(vector))
        self.assertTrue(scalar)                                       # the test data has flags

    def test_warmup_is_nan_not_none(self):
        close = pd.DataFrame({"X": np.linspace(100, 110, 30)})
        vol = pd.DataFrame({"X": [1000.0] * 30})
        panel = flags.flag_panels(close, vol)
        self.assertTrue(pd.isna(panel["X"].iloc[0]))                 # no window yet


class TestExhaustion(unittest.TestCase):
    def _frame(self, highs, lows, closes, vols):
        idx = pd.RangeIndex(len(closes))
        f = lambda a: pd.DataFrame({"X": a}, index=idx, dtype=float)
        return f(highs), f(lows), f(closes), f(vols)

    def test_selling_climax_is_seller(self):
        n = 30
        high = [101.0] * n; low = [99.0] * n; close = [100.0] * n; vol = [1000.0] * n
        # last bar: fresh low, volume spike, closes strong (upper part of range)
        low[-1] = 90.0; high[-1] = 100.0; close[-1] = 99.0; vol[-1] = 5000.0
        h, l, c, v = self._frame(high, low, close, vol)
        self.assertEqual(exhaustion.exhaustion_panels(h, l, c, v)["X"].iloc[-1], "seller")

    def test_buying_climax_is_buyer(self):
        n = 30
        high = [101.0] * n; low = [99.0] * n; close = [100.0] * n; vol = [1000.0] * n
        high[-1] = 112.0; low[-1] = 100.0; close[-1] = 101.0; vol[-1] = 5000.0
        h, l, c, v = self._frame(high, low, close, vol)
        self.assertEqual(exhaustion.exhaustion_panels(h, l, c, v)["X"].iloc[-1], "buyer")

    def test_no_spike_is_none(self):
        n = 30
        high = [101.0] * n; low = [99.0] * n; close = [100.0] * n; vol = [1000.0] * n
        low[-1] = 90.0; close[-1] = 99.0   # fresh low + strong close but NO volume spike
        h, l, c, v = self._frame(high, low, close, vol)
        self.assertEqual(exhaustion.exhaustion_panels(h, l, c, v)["X"].iloc[-1], "none")


def _ws(**over):
    """Neutral conviction context (wave factors zeroed) so the flag/exhaustion
    factors can be tested in isolation."""
    base = dict(trend="none", leg_kind="none", wave="—",
                cur=np.nan, c_tgt_hi=np.nan, w4_zone=np.nan,
                gp_q=0.0, ext_q=0.0, mtf_1mo=0.0, mtf_1d=0.0, mtf_1h=0.0,
                div_rs="none", div_px="none", flag=0.0, regime=None, vol_exh=None,
                flag_win_bull=None, flag_n_bull=0, flag_win_bear=None, flag_n_bear=0)
    base.update(over)
    return base


class TestConvictionFlagFactor(unittest.TestCase):
    def _score(self, **over):
        return signal._conviction(_ws(**over))[0]

    def test_regime_factor_table(self):
        self.assertEqual(signal._flag_regime_factor("bull", "HEALTHY"), 1.0)
        self.assertEqual(signal._flag_regime_factor("bull", "DETERIORATING"), signal.FLAG_OPPOSING)
        self.assertEqual(signal._flag_regime_factor("bear", "HEALTHY"), signal.FLAG_OPPOSING)
        self.assertEqual(signal._flag_regime_factor("bear", "DETERIORATING"), 1.0)
        self.assertEqual(signal._flag_regime_factor("bull", "NEUTRAL"), 1.0)
        self.assertEqual(signal._flag_regime_factor("bear", None), 1.0)

    def test_no_flag_is_zero(self):
        self.assertEqual(self._score(), 0.0)

    def test_basket_default_bull(self):
        # 150 * (FLAG_BASE_WIN.bull - 0.5) * 1.0 * 0.80
        expect = 150 * (signal.FLAG_BASE_WIN["bull"] - 0.5) * signal.TF_WEIGHT["1wk"]
        self.assertAlmostEqual(self._score(flag=1.0), round(expect, 1), places=1)

    def test_per_symbol_edge_scales(self):
        # 150 * (0.70-0.5) * 0.80 = 24.0 with enough events
        self.assertAlmostEqual(
            self._score(flag=1.0, flag_win_bull=0.70, flag_n_bull=20), 24.0, places=1)

    def test_thin_sample_falls_back_to_basket(self):
        # n below FLAG_MIN_N → ignore the per-symbol rate, use the basket default
        basket = self._score(flag=1.0)
        self.assertAlmostEqual(
            self._score(flag=1.0, flag_win_bull=0.95, flag_n_bull=3), basket, places=1)

    def test_opposing_regime_zeroes_the_flag(self):
        self.assertEqual(self._score(flag=1.0, regime="DETERIORATING"), 0.0)
        # a bear flag with a real per-symbol edge is zeroed in a HEALTHY regime
        self.assertEqual(
            self._score(flag=-1.0, flag_win_bear=0.65, flag_n_bear=15, regime="HEALTHY"), 0.0)

    def test_bear_flag_with_edge_is_bearish(self):
        # 150 * (0.65-0.5) * 0.80 = 18.0, bearish (negative)
        self.assertAlmostEqual(
            self._score(flag=-1.0, flag_win_bear=0.65, flag_n_bear=15), -18.0, places=1)

    def test_basket_bear_flag_has_no_measured_edge(self):
        # FLAG_BASE_WIN.bear ≤ 0.5 → edge clamps to 0 → contributes nothing
        self.assertEqual(self._score(flag=-1.0), 0.0)

    def test_exhaustion_signs(self):
        # W_VOL_EXH(14) * 0.80 = 11.2
        self.assertAlmostEqual(self._score(vol_exh="seller"), 11.2, places=1)
        self.assertAlmostEqual(self._score(vol_exh="buyer"), -11.2, places=1)

    def test_rotation_gate(self):
        # concentration regime suppresses conviction; broadening supports it
        self.assertAlmostEqual(self._score(rotation="off"), -signal.W_ROTATION_OFF, places=1)
        self.assertAlmostEqual(self._score(rotation="on"), signal.W_ROTATION_ON, places=1)
        self.assertEqual(self._score(rotation=None), 0.0)

    def test_rotation_gate_demotes_an_entry(self):
        # a setup that clears T_IN on its own falls below it once rotation is off
        ws_on  = _ws(flag=1.0, flag_win_bull=0.70, flag_n_bull=20, vol_exh="seller")
        base   = signal._conviction(ws_on)[0]
        gated  = signal._conviction(dict(ws_on, rotation="off"))[0]
        self.assertAlmostEqual(base - gated, signal.W_ROTATION_OFF, places=1)


class TestRotationLabel(unittest.TestCase):
    def test_on_when_equal_weight_leads(self):
        idx = pd.date_range("2024-01-01", periods=80)
        spy = pd.Series(np.full(80, 100.0), index=idx)
        rising  = pd.Series(np.linspace(100, 120, 80), index=idx)   # RSP/SPY rising
        falling = pd.Series(np.linspace(120, 100, 80), index=idx)   # RSP/SPY falling
        self.assertEqual(signal._rotation_label(rising, spy).iloc[-1], "on")
        self.assertEqual(signal._rotation_label(falling, spy).iloc[-1], "off")


if __name__ == "__main__":
    unittest.main()
