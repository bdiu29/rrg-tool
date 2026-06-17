"""Unit tests for the CANSLIM composing module — the pure letter scorers, the
market multiplier, the composite renormalization over available letters, and the
end-to-end leaderboard over a faked screener snapshot. No network."""

import unittest

import numpy as np
import pandas as pd

from modules import canslim as cs


class TestLetterScorers(unittest.TestCase):
    def test_growth_score_boundaries(self):
        self.assertEqual(cs._growth_score(0.0), 0.0)
        self.assertEqual(cs._growth_score(-0.1), 0.0)          # shrinking earnings → 0
        self.assertAlmostEqual(cs._growth_score(cs.GROWTH_TARGET), 75.0)
        self.assertEqual(cs._growth_score(2.0), 100.0)         # capped
        self.assertIsNone(cs._growth_score(None))
        self.assertIsNone(cs._growth_score(float("nan")))

    def test_near_high_score(self):
        self.assertEqual(cs._near_high_score(0), 100.0)        # at the high
        self.assertEqual(cs._near_high_score(-3), 100.0)       # within the full band
        self.assertEqual(cs._near_high_score(-35), 0.0)        # far below
        # halfway between the full and zero bands → mid score
        mid = cs._near_high_score(-(cs.NEAR_HIGH_FULL + cs.NEAR_HIGH_ZERO) / 2)
        self.assertTrue(40 < mid < 60)
        self.assertIsNone(cs._near_high_score(None))

    def test_supply_demand(self):
        self.assertEqual(cs._supply_demand_score("A"), 100.0)
        self.assertEqual(cs._supply_demand_score("E"), 0.0)
        self.assertIsNone(cs._supply_demand_score("none"))
        self.assertIsNone(cs._supply_demand_score(None))
        # a small float (low percentile) lifts the score above the demand-only read
        self.assertGreater(cs._supply_demand_score("C", float_pctl=5),
                           cs._supply_demand_score("C", float_pctl=95))

    def test_market_factor(self):
        self.assertEqual(cs._market_factor("HEALTHY"), 1.0)
        self.assertEqual(cs._market_factor("DETERIORATING"), 0.6)
        self.assertEqual(cs._market_factor(None), 0.85)        # unknown → neutral default


class TestComposite(unittest.TestCase):
    def test_renormalizes_over_available_letters(self):
        # all-80 over five present letters (I missing) → 80, not diluted by the gap
        scores = {"C": 80, "A": 80, "N": 80, "S": 80, "L": 80, "I": None}
        self.assertAlmostEqual(cs._composite(scores), 80.0)

    def test_weights_tilt_toward_L_and_C(self):
        hi_L = cs._composite({"C": 50, "A": 50, "N": 50, "S": 50, "L": 100, "I": None})
        hi_N = cs._composite({"C": 50, "A": 50, "N": 100, "S": 50, "L": 50, "I": None})
        self.assertGreater(hi_L, hi_N)                          # L carries more weight than N

    def test_no_letters_is_none(self):
        self.assertIsNone(cs._composite({k: None for k in cs.LETTER_WEIGHTS}))


class TestScoreStock(unittest.TestCase):
    def test_full_scorecard_and_pass_flags(self):
        row = {"eps_growth_q": 0.40, "eps_growth_a": 0.30,
               "pct_from_52w_high": -3, "ad_rating": "A"}
        card = cs.score_stock(row, rs_rating=88, float_pctl=20)
        lt = card["letters"]
        self.assertTrue(lt["C"]["pass"] and lt["N"]["pass"] and lt["L"]["pass"])
        self.assertIsNone(lt["I"])                              # institutional pending
        self.assertGreater(card["composite_raw"], 80)

    def test_missing_fundamentals_drop_out(self):
        # only price-side data → C/A absent, composite still computed over N/S/L
        row = {"pct_from_52w_high": -2, "ad_rating": "B"}
        card = cs.score_stock(row, rs_rating=70)
        self.assertIsNone(card["letters"]["C"])
        self.assertIsNone(card["letters"]["A"])
        self.assertIsNotNone(card["composite_raw"])


class TestComputeCanslim(unittest.TestCase):
    """End-to-end over a faked screener snapshot — covers the RS percentile, the
    market factor, sorting, and the limit, with no DB/network."""

    def setUp(self):
        from modules.screener import store as scr_store
        from modules.breadth import store as breadth_store
        self.scr_store, self.breadth_store = scr_store, breadth_store
        self._orig = (scr_store.get_snapshot, scr_store.get_fundamentals,
                      breadth_store.get_members, cs.signal.current_regime,
                      scr_store.get_inst_ownership)

        idx = pd.Index(["LEAD", "MID", "LAG"], name="symbol")
        snap = pd.DataFrame({
            "date": ["2026-06-15"] * 3,
            "close": [100.0, 50.0, 20.0],
            "pct_from_52w_high": [-1.0, -12.0, -40.0],
            "rs_1m_pct": [8.0, 1.0, -6.0],
            "rs_3m_pct": [15.0, 2.0, -10.0],
            "ad_rating": ["A", "C", "E"],
        }, index=idx)
        fund = pd.DataFrame({
            "shares_outstanding": [1e8, 5e8, 9e8],
            "eps_growth_q": [0.45, 0.10, -0.20],
            "eps_growth_a": [0.35, 0.08, -0.15],
            "sector": ["Technology", "Financials", "Energy"],
        }, index=idx)

        # institutional ownership: LEAD has a rising QoQ read (scores I); MID absent
        own = pd.DataFrame({
            "pct_held":           [0.72],
            "pct_held_prev":      [0.60],
            "holders_count":      [1300.0],
            "holders_count_prev": [1000.0],
        }, index=pd.Index(["LEAD"], name="symbol"))

        scr_store.get_snapshot = lambda: snap
        scr_store.get_fundamentals = lambda: fund
        scr_store.get_inst_ownership = lambda: own
        breadth_store.get_members = lambda u, **k: {"LEAD", "MID", "LAG"}
        cs.signal.current_regime = lambda: "HEALTHY"

    def tearDown(self):
        (self.scr_store.get_snapshot, self.scr_store.get_fundamentals,
         self.breadth_store.get_members, cs.signal.current_regime,
         self.scr_store.get_inst_ownership) = self._orig

    def test_leaderboard_ranks_the_growth_leader_first(self):
        rep = cs.compute_canslim("sp500", limit=10)
        syms = [s["symbol"] for s in rep["stocks"]]
        self.assertEqual(syms[0], "LEAD")                       # strongest on every letter
        self.assertEqual(syms, sorted(syms, key=lambda s: -dict(
            zip(syms, [x["composite"] for x in rep["stocks"]]))[s]))
        self.assertGreater(rep["stocks"][0]["composite"], rep["stocks"][-1]["composite"])
        self.assertEqual(rep["market"]["regime"], "HEALTHY")

    def test_rs_rating_is_cross_sectional_percentile(self):
        rep = cs.compute_canslim("sp500", limit=10)
        ratings = {s["symbol"]: s["rs_rating"] for s in rep["stocks"]}
        self.assertGreater(ratings["LEAD"], ratings["LAG"])
        for v in ratings.values():
            self.assertTrue(0 <= v <= 99)

    def test_deteriorating_market_dampens_scores(self):
        cs.signal.current_regime = lambda: "DETERIORATING"
        rep = cs.compute_canslim("sp500", limit=10)
        self.assertEqual(rep["market"]["factor"], 0.6)
        # the leader's composite is scaled down vs the healthy run
        self.assertLess(rep["stocks"][0]["composite"], 80)

    def test_limit_caps_the_board(self):
        rep = cs.compute_canslim("sp500", limit=2)
        self.assertEqual(len(rep["stocks"]), 2)
        self.assertEqual(rep["total"], 3)

    def test_institutional_letter_scores_when_ownership_present(self):
        rep = cs.compute_canslim("sp500", limit=10)
        by_sym = {s["symbol"]: s for s in rep["stocks"]}
        # LEAD has a rising-ownership read → I letter scored & passing
        self.assertIsNotNone(by_sym["LEAD"]["letters"]["I"])
        self.assertTrue(by_sym["LEAD"]["letters"]["I"]["pass"])
        # MID has no ownership read → I stays None (forward-accumulating)
        self.assertIsNone(by_sym["MID"]["letters"]["I"])


if __name__ == "__main__":
    unittest.main()
