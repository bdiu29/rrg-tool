"""Unit tests for the options-flow scoring engine (the trader's 6-rule filter)."""

import unittest

from modules.flow import scoring


def _contract(**over):
    c = dict(put_call="CALL", spot=100.0, strike=105.0, dte=45,
             session_volume=20000, volume_delta=5000, open_interest=500,
             bid=2.0, ask=2.1, last=2.1, mark=2.05, cluster_count=3,
             baseline_notional=1_000_000, confluence=None)
    c.update(over)
    return c


class TestHardGates(unittest.TestCase):
    def test_churn_vol_below_oi_is_noise(self):
        r = scoring.classify_contract(_contract(session_volume=2000, open_interest=50000, mark=2.0))
        self.assertEqual(r["classification"], "noise")
        self.assertIn("churn", r["drop_reason"])

    def test_tiny_size_is_noise(self):
        r = scoring.classify_contract(_contract(session_volume=100, mark=1.0, open_interest=1))
        self.assertEqual(r["classification"], "noise")
        self.assertIn("size", r["drop_reason"])

    def test_0dte_dropped_without_confluence(self):
        r = scoring.classify_contract(_contract(dte=0))
        self.assertEqual(r["drop_reason"], "0DTE lotto")

    def test_0dte_survives_with_strong_confluence(self):
        r = scoring.classify_contract(_contract(
            dte=0, confluence={"sector_call": "ROTATE IN", "vp_zone": "discount"}))
        self.assertIsNone(r["drop_reason"])

    def test_insane_otm_is_noise(self):
        r = scoring.classify_contract(_contract(strike=200.0))   # +100% OTM
        self.assertIn("OTM", r["drop_reason"])


class TestConvictionGrading(unittest.TestCase):
    def test_serious_voloi_big_size_swing_cluster_is_conviction(self):
        r = scoring.classify_contract(_contract(
            confluence={"sector_call": "ROTATE IN", "vp_zone": "discount"}))
        self.assertEqual(r["classification"], "conviction")
        self.assertGreaterEqual(r["conviction"], scoring.T_CONVICTION)
        self.assertEqual(r["direction"], "bullish")
        self.assertAlmostEqual(r["vol_oi_ratio"], 40.0)

    def test_confluence_lifts_score(self):
        bare = scoring.classify_contract(_contract(confluence=None))
        aligned = scoring.classify_contract(_contract(
            confluence={"sector_call": "ROTATE IN", "vp_zone": "discount"}))
        self.assertGreater(aligned["conviction"], bare["conviction"])

    def test_put_is_bearish(self):
        r = scoring.classify_contract(_contract(put_call="PUT"))
        self.assertEqual(r["direction"], "bearish")

    def test_weak_interesting_voloi_is_not_conviction(self):
        # 6× OI, modest size, no cluster, no confluence → notable/watch at most.
        r = scoring.classify_contract(_contract(
            session_volume=3000, open_interest=500, mark=2.0,
            cluster_count=1, baseline_notional=2_000_000, confluence=None))
        self.assertIn(r["classification"], ("watch", "notable"))
        self.assertLess(r["conviction"], scoring.T_CONVICTION)


class TestHelpers(unittest.TestCase):
    def test_estimate_aggressor(self):
        self.assertEqual(scoring.estimate_aggressor(2.2, 2.0, 2.1), "above_ask")
        self.assertEqual(scoring.estimate_aggressor(2.1, 2.0, 2.1), "ask")
        self.assertEqual(scoring.estimate_aggressor(2.05, 2.0, 2.1), "mid")
        self.assertEqual(scoring.estimate_aggressor(2.0, 2.0, 2.1), "bid")
        self.assertEqual(scoring.estimate_aggressor(1.9, 2.0, 2.1), "below_bid")
        self.assertEqual(scoring.estimate_aggressor(None, 2.0, 2.1), "unknown")

    def test_expiry_bucket(self):
        self.assertEqual(scoring.expiry_bucket(0), "0dte")
        self.assertEqual(scoring.expiry_bucket(7), "weekly")
        self.assertEqual(scoring.expiry_bucket(45), "swing")
        self.assertEqual(scoring.expiry_bucket(400), "leaps")

    def test_aggressor_method_defaults_to_estimated(self):
        r = scoring.classify_contract(_contract())
        self.assertEqual(r["aggressor_method"], "estimated")

    def test_polygon_confirmed_aggressor_passes_through(self):
        r = scoring.classify_contract(_contract(aggressor="above_ask", aggressor_method="confirmed"))
        self.assertEqual(r["aggressor"], "above_ask")
        self.assertEqual(r["aggressor_method"], "confirmed")


if __name__ == "__main__":
    unittest.main()
