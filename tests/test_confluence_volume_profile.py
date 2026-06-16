"""Unit tests for the volume-profile confluence factor and its wiring into the RRG
conviction engine. Pure/synthetic — no network."""

import json
import unittest

import numpy as np
import pandas as pd

from modules.confluence import volume_profile as vp
from modules.rrg import signal


def _gauss(n, mu, sigma):
    i = np.arange(n, dtype=float)
    return np.exp(-((i - mu) ** 2) / (2.0 * sigma ** 2))


class TestHistogram(unittest.TestCase):
    def test_poc_lands_on_the_high_volume_price(self):
        # Five broad low-volume bars spanning 100–110, plus one tight bar with huge
        # volume at ~105 → the POC must sit on 105.
        high = [110, 110, 110, 110, 110, 105.1]
        low  = [100, 100, 100, 100, 100, 104.9]
        close = [105, 105, 105, 105, 105, 105]
        vol  = [1, 1, 1, 1, 1, 100]
        centers, vbins, lo, hi = vp.build_profile(high, low, close, vol, bins=50)
        poc = centers[int(np.argmax(vbins))]
        self.assertLess(abs(poc - 105), 1.0)
        self.assertEqual((lo, hi), (100.0, 110.0))

    def test_no_volume_returns_none(self):
        out = vp.build_profile([100], [99], [99.5], [0], bins=10)
        self.assertEqual(out, (None, None, None, None))

    def test_value_area_brackets_the_poc(self):
        centers = np.arange(50, dtype=float)
        vbins = _gauss(50, 24.5, 4.0)
        poc, val, vah, *_ = vp.value_area(centers, vbins, coverage=0.70)
        self.assertLessEqual(val, poc)
        self.assertLessEqual(poc, vah)
        self.assertLess(vah - val, 25)          # a sharp peak → a narrow value area


class TestShapeClassifier(unittest.TestCase):
    def setUp(self):
        self.centers = np.arange(50, dtype=float)

    def test_balanced_is_D(self):
        self.assertEqual(vp.classify_shape(self.centers, _gauss(50, 24.5, 5)), "D")

    def test_fat_top_is_P(self):
        self.assertEqual(vp.classify_shape(self.centers, _gauss(50, 44, 5)), "P")

    def test_fat_bottom_is_b(self):
        self.assertEqual(vp.classify_shape(self.centers, _gauss(50, 5, 5)), "b")

    def test_double_distribution_is_B(self):
        vbins = _gauss(50, 12, 3) + _gauss(50, 37, 3)
        self.assertEqual(vp.classify_shape(self.centers, vbins), "B")

    def test_flat_is_trend(self):
        self.assertEqual(vp.classify_shape(self.centers, np.ones(50)), "trend")


class TestCurrentRead(unittest.TestCase):
    def _series(self, n_at_100, last_price):
        # n_at_100 bars parked at ~100 to build the value area, then one bar at
        # `last_price` to test which zone it lands in.
        highs = [100.5] * n_at_100 + [last_price + 0.1]
        lows  = [99.5] * n_at_100 + [last_price - 0.1]
        close = [100.0] * n_at_100 + [last_price]
        vol   = [1000] * n_at_100 + [10]
        idx = pd.RangeIndex(n_at_100 + 1)
        return (pd.Series(highs, idx), pd.Series(lows, idx),
                pd.Series(close, idx), pd.Series(vol, idx))

    def test_zone_discount_premium_value(self):
        h, l, c, v = self._series(40, 80)
        self.assertEqual(vp.current(h, l, c, v)["zone"], "discount")
        h, l, c, v = self._series(40, 130)
        self.assertEqual(vp.current(h, l, c, v)["zone"], "premium")
        h, l, c, v = self._series(40, 100)
        self.assertEqual(vp.current(h, l, c, v)["zone"], "value")

    def test_read_is_json_safe(self):
        h, l, c, v = self._series(40, 80)
        read = vp.current(h, l, c, v)
        json.dumps(read)                          # must not raise on numpy scalars
        for k in ("poc", "vah", "val"):
            self.assertIsInstance(read[k], float)

    def test_empty_returns_none(self):
        e = pd.Series([], dtype=float)
        self.assertIsNone(vp.current(e, e, e, e))


class TestContribution(unittest.TestCase):
    def test_discount_is_bullish_premium_bearish(self):
        s_disc, _ = vp.contribution({"zone": "discount", "shape": "b"})
        s_prem, _ = vp.contribution({"zone": "premium", "shape": "P"})
        s_val, _  = vp.contribution({"zone": "value", "shape": "D"})
        self.assertGreater(s_disc, 0)
        self.assertLess(s_prem, 0)
        self.assertEqual(s_val, 0.0)

    def test_shape_reinforces_zone(self):
        s_plain, _ = vp.contribution({"zone": "discount", "shape": "D"})
        s_boost, _ = vp.contribution({"zone": "discount", "shape": "b"})
        self.assertGreater(s_boost, s_plain)

    def test_none_read_is_neutral(self):
        self.assertEqual(vp.contribution(None), (0.0, ""))


class TestConvictionWiring(unittest.TestCase):
    """The factor must shift the RRG conviction score the right way, and absence
    (the backtest/themes path) must leave it untouched."""

    BASE = {"trend": "up", "leg_kind": "corrective", "wave": "wave-2",
            "cur": float("nan"), "gp_q": 0.0, "ext_q": 0.0}

    def test_discount_lifts_premium_lowers(self):
        s0, _ = signal._conviction(dict(self.BASE))
        s_disc, _ = signal._conviction({**self.BASE,
                                        "vol_profile": {"zone": "discount", "shape": "b"}})
        s_prem, _ = signal._conviction({**self.BASE,
                                        "vol_profile": {"zone": "premium", "shape": "P"}})
        self.assertGreater(s_disc, s0)
        self.assertLess(s_prem, s0)

    def test_absent_profile_is_inert(self):
        s_none, f_none = signal._conviction(dict(self.BASE))
        self.assertNotIn("VP", json.dumps(f_none))


if __name__ == "__main__":
    unittest.main()
