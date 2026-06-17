"""Unit tests for the accumulation/distribution confluence factor and its wiring
into the RRG conviction engine. Pure/synthetic — no network."""

import json
import unittest

import numpy as np
import pandas as pd

from modules.confluence import accumulation as acc
from modules.rrg import signal


def _tape(n, up_vol, down_vol, drift, close_pos=0.5, seed=0):
    """Synthetic OHLCV: a drifting close where up-close bars carry `up_vol` and
    down-close bars `down_vol`. `close_pos` places the close within each bar's
    range (0=low, 1=high) for the A/D-line / big-day reads."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = pd.Series(100 * np.cumprod(1 + rng.normal(drift, 0.008, n)), idx)
    rng_w = 0.02 * close
    high = close + (1 - close_pos) * rng_w
    low = close - close_pos * rng_w
    up = close.diff() > 0
    vol = pd.Series(np.where(up, up_vol, down_vol), idx).astype(float)
    return high, low, close, vol


class TestStrengthScoring(unittest.TestCase):
    def test_high_ud_is_bullish_low_is_bearish(self):
        self.assertGreater(acc._strength(2.0, 1.0, 1.0, "confirming"), 0)
        self.assertLess(acc._strength(0.4, -1.0, -1.0, "confirming"), 0)

    def test_balanced_is_neutral(self):
        self.assertAlmostEqual(float(acc._strength(1.0, 0.0, 0.0, "confirming")), 0.0)

    def test_distribution_divergence_pulls_down(self):
        base = float(acc._strength(1.2, 0.0, 0.0, "confirming"))
        diverged = float(acc._strength(1.2, 0.0, 0.0, "distribution"))
        self.assertLess(diverged, base)

    def test_rating_letters_span_A_to_E(self):
        self.assertEqual(str(acc._rating(0.9)), "A")
        self.assertEqual(str(acc._rating(0.3)), "B")
        self.assertEqual(str(acc._rating(0.0)), "C")
        self.assertEqual(str(acc._rating(-0.3)), "D")
        self.assertEqual(str(acc._rating(-0.9)), "E")


class TestCurrentRead(unittest.TestCase):
    def test_accumulation_tape_reads_bullish(self):
        h, l, c, v = _tape(120, up_vol=2_000_000, down_vol=600_000, drift=0.002, close_pos=0.85)
        r = acc.current(h, l, c, v)
        self.assertGreater(r["ud_ratio"], 1.0)
        self.assertIn(r["rating"], ("A", "B"))
        s, lab = acc.contribution(r)
        self.assertGreater(s, 0)
        self.assertIn("A/D", lab)

    def test_distribution_tape_reads_bearish(self):
        h, l, c, v = _tape(120, up_vol=600_000, down_vol=2_500_000, drift=-0.001, close_pos=0.15)
        r = acc.current(h, l, c, v)
        self.assertLess(r["ud_ratio"], 1.0)
        self.assertIn(r["rating"], ("D", "E"))
        s, _ = acc.contribution(r)
        self.assertLess(s, 0)

    def test_price_up_with_weak_closes_flags_distribution_divergence(self):
        # price drifts UP but each bar closes near its low → A/D line falls
        n = 120
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        base = pd.Series(100 + np.linspace(0, 12, n), idx)
        high, low = base * 1.02, base * 0.98
        close = low + 0.10 * (high - low)          # close near the low
        vol = pd.Series(1_000_000.0, idx)
        r = acc.current(high, low, close, vol)
        self.assertEqual(r["divergence"], "distribution")
        self.assertLess(r["adl_dir"], 0)

    def test_read_is_json_safe(self):
        h, l, c, v = _tape(120, 2_000_000, 600_000, 0.002, 0.8)
        json.dumps(acc.current(h, l, c, v))        # numpy scalars must serialize

    def test_short_input_returns_none(self):
        h, l, c, v = _tape(30, 1_000_000, 1_000_000, 0.0)
        self.assertIsNone(acc.current(h, l, c, v))

    def test_empty_returns_none(self):
        e = pd.Series([], dtype=float)
        self.assertIsNone(acc.current(e, e, e, e))


class TestPanels(unittest.TestCase):
    def test_panel_matches_current_at_last_bar(self):
        h, l, c, v = _tape(120, 2_000_000, 600_000, 0.002, 0.85)
        pan = acc.panels(h.to_frame("X"), l.to_frame("X"), c.to_frame("X"), v.to_frame("X"))
        self.assertEqual(pan["X"].iloc[-1], acc.current(h, l, c, v)["rating"])

    def test_unformed_window_is_nan(self):
        h, l, c, v = _tape(120, 2_000_000, 600_000, 0.002, 0.85)
        pan = acc.panels(h.to_frame("X"), l.to_frame("X"), c.to_frame("X"), v.to_frame("X"))
        # first WINDOW+VOL_AVG-ish rows can't be fully formed → NaN, not a letter
        self.assertTrue(pan["X"].iloc[:acc.WINDOW].isna().any())


class TestContribution(unittest.TestCase):
    def test_none_read_is_neutral(self):
        self.assertEqual(acc.contribution(None), (0.0, ""))

    def test_divergence_appears_in_label(self):
        read = {"ud_ratio": 0.3, "adl_dir": -1.0, "divergence": "distribution",
                "accum_days": 0, "distrib_days": 20, "rating": "E"}
        s, lab = acc.contribution(read)
        self.assertLess(s, 0)
        self.assertIn("distribution", lab)


class TestConvictionWiring(unittest.TestCase):
    """The factor must shift the RRG conviction score the right way, and absence
    (the backtest/themes path) must leave it untouched."""

    BASE = {"trend": "up", "leg_kind": "corrective", "wave": "wave-2",
            "cur": float("nan"), "gp_q": 0.0, "ext_q": 0.0}

    ACCUM = {"ud_ratio": 3.0, "adl_dir": 1.0, "divergence": "confirming",
             "accum_days": 20, "distrib_days": 0, "rating": "A"}
    DISTRIB = {"ud_ratio": 0.3, "adl_dir": -1.0, "divergence": "distribution",
               "accum_days": 0, "distrib_days": 20, "rating": "E"}

    def test_accumulation_lifts_distribution_lowers(self):
        s0, _ = signal._conviction(dict(self.BASE))
        s_acc, _ = signal._conviction({**self.BASE, "accumulation": self.ACCUM})
        s_dis, _ = signal._conviction({**self.BASE, "accumulation": self.DISTRIB})
        self.assertGreater(s_acc, s0)
        self.assertLess(s_dis, s0)

    def test_absent_accumulation_is_inert(self):
        _, f_none = signal._conviction(dict(self.BASE))
        self.assertNotIn("A/D", json.dumps(f_none))


if __name__ == "__main__":
    unittest.main()
