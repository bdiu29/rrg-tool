"""Unit tests for the institutional-sponsorship confluence leaf (CANSLIM's I).
Pure — ownership numbers in, signed contribution / 0-100 score out."""

import unittest

from modules.confluence import institutional as inst


class TestCurrent(unittest.TestCase):
    def test_level_only_when_no_prior(self):
        r = inst.current(0.60)
        self.assertEqual(r["pct_held"], 0.60)
        self.assertIsNone(r["delta"])
        self.assertEqual(r["trend"], "unknown")

    def test_qoq_trend(self):
        self.assertEqual(inst.current(0.65, 0.60)["trend"], "accumulating")
        self.assertEqual(inst.current(0.55, 0.60)["trend"], "distributing")
        self.assertEqual(inst.current(0.60, 0.60)["trend"], "flat")

    def test_none_when_no_pct(self):
        self.assertIsNone(inst.current(None))
        self.assertIsNone(inst.current(float("nan")))


class TestContribution(unittest.TestCase):
    def test_rising_ownership_is_bullish(self):
        s, lab = inst.contribution(inst.current(0.70, 0.60))
        self.assertGreater(s, 0)
        self.assertIn("accumulating", lab)

    def test_falling_ownership_is_bearish(self):
        s, _ = inst.contribution(inst.current(0.50, 0.62))
        self.assertLess(s, 0)

    def test_lacking_sponsorship_is_negative(self):
        s, _ = inst.contribution(inst.current(0.05))     # 5% held, no prior
        self.assertLess(s, 0)

    def test_crowded_is_capped_negative(self):
        s, _ = inst.contribution(inst.current(0.97))     # ~fully owned, no room
        self.assertLessEqual(s, 0)

    def test_healthy_band_mild_positive(self):
        s, _ = inst.contribution(inst.current(0.60))     # healthy, no prior
        self.assertGreater(s, 0)

    def test_holder_growth_adds(self):
        base = inst.contribution(inst.current(0.60, 0.60))[0]
        grew = inst.contribution(inst.current(0.60, 0.60, holders=1200, holders_prev=1000))[0]
        self.assertGreater(grew, base)

    def test_none_read_is_neutral(self):
        self.assertEqual(inst.contribution(None), (0.0, ""))


class TestScore(unittest.TestCase):
    def test_score_maps_to_0_100(self):
        hi = inst.score(inst.current(0.72, 0.60, holders=1300, holders_prev=1000))
        lo = inst.score(inst.current(0.45, 0.62))
        self.assertTrue(0 <= lo <= 100 and 0 <= hi <= 100)
        self.assertGreater(hi, lo)

    def test_none_read_scores_none(self):
        self.assertIsNone(inst.score(None))


if __name__ == "__main__":
    unittest.main()
