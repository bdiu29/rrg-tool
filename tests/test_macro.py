"""
Macro module tests — the deterministic regime softmax, the indicator state
classification, the feature blend, and fail-soft behavior. No network: a synthetic
`raw` payload stands in for sources.fetch_raw, and the harness vote is exercised
against a stubbed build_dashboard.
"""

import unittest
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd

from modules.macro import indicators, regime, sources


def _series(values):
    idx = pd.bdate_range(end=date(2026, 6, 17), periods=len(values))
    return pd.Series(np.asarray(values, dtype=float), index=idx)


def _synthetic_raw():
    """A deterministic, network-free `raw` dict shaped like sources.fetch_raw."""
    n = 300
    idx = pd.bdate_range(end=date(2026, 6, 17), periods=n)
    t = np.arange(n)
    cols = {
        "SPY": 400 + t * 0.4,                      # steady uptrend
        "RSP": 160 + t * 0.1,
        "IWM": 200 + t * 0.1,
        "^VIX": np.full(n, 12.0),                  # very low → COMPLACENT
        "^VIX3M": np.full(n, 15.0),                # contango (0.8) → STABLE
        "XLK": 200 + t * 0.5, "XLY": 180 + t * 0.3, "XLC": 70 + t * 0.1,
        "XLU": 70 + t * 0.02, "XLP": 75 + t * 0.02, "XLV": 130 + t * 0.05,
        "XLE": 90 + t * 0.08, "XLF": 40 + t * 0.06, "XLI": 120 + t * 0.1,
        "HG=F": 4.0 + np.sin(t / 30) * 0.1, "GC=F": 2300 + t * 0.5,
        "HYG": 79 + t * 0.01,                      # credit keeping pace → STABLE
        "DX-Y.NYB": 103 - t * 0.005,               # dollar easing → STABLE
        "BTC-USD": 60000 + t * 60,                 # rising liquidity → STABLE
    }
    close = pd.DataFrame(cols, index=idx)
    fred = {
        "T10Y2Y": _series(np.full(60, 0.30)),               # normal curve → STABLE
        "T10Y3M": _series(np.full(60, 0.50)),
        "DGS10":  _series(np.full(60, 4.20)),
        "DFII10": _series(np.full(60, 1.90)),
        "T10YIE": _series(np.full(60, 2.30)),
        "BAMLH0A0HYM2": _series(np.full(60, 3.00)),         # tight credit → STABLE
        "ICSA":   _series(np.full(60, 230000.0)),           # healthy labor → STABLE
    }
    breadth = {"regime": "NEUTRAL", "metrics": {
        "pct_above_50": 55.0, "pct_above_200": 58.0, "mcclellan": 10.0,
        "nh_nl": 1.5, "advances": 1500.0, "declines": 1300.0,
    }}
    return {"close": close, "fred": fred, "breadth": breadth, "as_of": "2026-06-17",
            "ok": {"market": True, "fred": True, "breadth": True}}


# ---------------------------------------------------------------------------
# regime.classify — the 4-quadrant softmax
# ---------------------------------------------------------------------------

class TestRegimeClassify(unittest.TestCase):

    def _top(self, g, i):
        return regime.classify({"growth_z": g, "inflation_z": i,
                                "growth_inputs": [], "inflation_inputs": []})

    def test_four_quadrants(self):
        self.assertEqual(self._top(2, -2)["regime"], "Goldilocks")    # growth↑ inflation↓
        self.assertEqual(self._top(2, 2)["regime"],  "Reflation")     # both ↑
        self.assertEqual(self._top(-2, 2)["regime"], "Stagflation")   # growth↓ inflation↑
        self.assertEqual(self._top(-2, -2)["regime"], "Disinflation") # both ↓

    def test_probabilities_sum_to_one(self):
        r = self._top(0.7, -0.4)
        total = sum(p["prob"] for p in r["probabilities"])
        self.assertTrue(99 <= total <= 101)                          # rounding tolerance
        self.assertEqual(len(r["probabilities"]), 4)

    def test_equity_tilt_sign(self):
        self.assertGreater(self._top(2, -2)["equity_tilt"], 0)        # Goldilocks supportive
        self.assertLess(self._top(-2, 2)["equity_tilt"], 0)          # Stagflation defensive

    def test_neutral_is_high_shift_risk(self):
        r = self._top(0, 0)
        self.assertEqual(r["confidence"], 25)                        # all four equal
        self.assertEqual(r["shift_risk"], "High")

    def test_confident_read_is_low_shift_risk(self):
        r = self._top(3, -3)
        self.assertGreater(r["confidence"], 60)
        self.assertEqual(r["shift_risk"], "Low")

    def test_driver_uses_strongest_inputs(self):
        r = regime.classify({"growth_z": 1.0, "inflation_z": 0.2,
                             "growth_inputs": [["Copper/gold", 2.4], ["Small caps", 0.1]],
                             "inflation_inputs": [["Gold", 0.3]]})
        self.assertTrue(r["driver"].startswith("Copper/gold"))


# ---------------------------------------------------------------------------
# indicators — state classification + feature blend
# ---------------------------------------------------------------------------

class TestIndicators(unittest.TestCase):

    def setUp(self):
        self.raw = _synthetic_raw()
        self.panels = indicators.build_indicators(self.raw)
        self.by_key = {r["key"]: r for r in self.panels["leading"] + self.panels["macro"]}

    def test_known_states(self):
        self.assertEqual(self.by_key["volatility_vix"]["state"], "COMPLACENT")  # VIX 12
        self.assertEqual(self.by_key["credit_spreads"]["state"], "STABLE")      # HY OAS 3.0
        self.assertEqual(self.by_key["yield_curve"]["state"], "STABLE")         # +0.30
        self.assertEqual(self.by_key["jobless_claims"]["state"], "STABLE")      # 230K
        self.assertEqual(self.by_key["market_breadth"]["state"], "WATCH")       # 55% above 50d
        self.assertEqual(self.by_key["vix_term_structure"]["state"], "STABLE")  # 12/15 contango

    def test_every_row_is_well_formed(self):
        for r in self.panels["leading"] + self.panels["macro"]:
            self.assertIn(r["state"], ("STABLE", "COMPLACENT", "WATCH", "TURNED"))
            self.assertTrue(r["meaning"])
            self.assertIsInstance(r["label"], str)
            self.assertTrue(r["value"] is None or isinstance(r["value"], float))

    def test_advance_decline_uses_counts(self):
        ad = self.by_key["advance_decline"]
        self.assertAlmostEqual(ad["value"], 1500 / 1300, places=2)

    def test_liquidity_credit_indicators_present(self):
        for key in ("hyg_spy", "dxy", "bitcoin"):
            self.assertIn(key, self.by_key)
            self.assertIn(self.by_key[key]["state"], ("STABLE", "COMPLACENT", "WATCH", "TURNED"))

    def test_hyg_divergence_flags_caution(self):
        # SPY rising while HYG falls = credit not confirming → WATCH.
        raw = _synthetic_raw()
        n = len(raw["close"])
        raw["close"]["HYG"] = pd.Series(
            np.linspace(85, 79, n), index=raw["close"].index)        # HYG trending down
        rows = {r["key"]: r for r in indicators.build_indicators(raw)["macro"]}
        self.assertEqual(rows["hyg_spy"]["state"], "WATCH")

    def test_dollar_feeds_both_regime_axes(self):
        f = indicators.regime_features(self.raw)
        labels = ([l for l, _ in f["growth_inputs"]] + [l for l, _ in f["inflation_inputs"]])
        self.assertEqual(sum(1 for l in labels if l == "US dollar (weaker)"), 2)
        self.assertIn("Bitcoin liquidity", [l for l, _ in f["growth_inputs"]])

    def test_feature_blend_keys(self):
        f = indicators.regime_features(self.raw)
        for k in ("growth_z", "inflation_z", "growth_inputs", "inflation_inputs"):
            self.assertIn(k, f)
        self.assertIsInstance(f["growth_z"], float)
        # classify must accept the blend without raising
        self.assertIn(regime.classify(f)["regime"], regime.ORDER)

    def test_panel_summary_variants(self):
        self.assertIn("No data", indicators._panel_summary([], "leading"))
        healthy = [{"state": "STABLE"}, {"state": "STABLE"}]
        self.assertIn("healthy", indicators._panel_summary(healthy, "leading"))


# ---------------------------------------------------------------------------
# Fail-soft — empty inputs must never raise
# ---------------------------------------------------------------------------

class TestFailSoft(unittest.TestCase):

    def test_empty_raw(self):
        empty = {"close": pd.DataFrame(), "fred": {}, "breadth": None,
                 "as_of": None, "ok": {"market": False, "fred": False, "breadth": False}}
        panels = indicators.build_indicators(empty)
        self.assertEqual(panels["leading"], [])
        self.assertEqual(panels["macro"], [])
        feats = indicators.regime_features(empty)
        self.assertEqual(feats["growth_z"], 0.0)
        self.assertEqual(feats["inflation_z"], 0.0)
        # a flat blend still classifies (NEUTRAL-ish, high shift risk)
        self.assertEqual(regime.classify(feats)["shift_risk"], "High")

    def test_dashboard_end_to_end(self):
        from modules import macro
        with mock.patch.object(sources, "fetch_raw", return_value=_synthetic_raw()):
            d = macro.build_dashboard()
        self.assertIn(d["regime"]["regime"], regime.ORDER)
        self.assertTrue(d["leading"])
        self.assertIn("health", d)
        self.assertIsNotNone(d["health"].get("spy_vs_50"))
        # 'What You Need to Know' cards — the Livermore set
        keys = [c["key"] for c in d["cards"]]
        self.assertEqual(keys, ["where", "too_late", "buy_dip", "when_end", "hidden"])
        for c in d["cards"]:
            self.assertTrue(c["headline"] and c["body"])


# ---------------------------------------------------------------------------
# sources helpers
# ---------------------------------------------------------------------------

class TestSourcesHelpers(unittest.TestCase):

    def test_col_any_picks_first_present(self):
        df = pd.DataFrame({"CPER": [1.0, 2.0, 3.0]})
        self.assertIsNone(sources.col_any(df, "HG=F"))
        self.assertIsNotNone(sources.col_any(df, "HG=F", "CPER"))

    def test_ratio_and_basket(self):
        df = pd.DataFrame({"A": [10.0, 11.0, 12.0], "B": [5.0, 5.0, 6.0]})
        r = sources.ratio(df, "A", "B")
        self.assertAlmostEqual(float(r.iloc[0]), 2.0)
        bk = sources.basket(df, ["A", "B"])
        self.assertAlmostEqual(float(bk.iloc[0]), 100.0)              # base 100

    def test_zscore_none_on_short_series(self):
        self.assertIsNone(sources.zscore_mom(_series(np.arange(10)), win=20))
        self.assertIsNone(sources.pct_change_n(_series([1.0, 2.0]), 5))


# ---------------------------------------------------------------------------
# Harness vote
# ---------------------------------------------------------------------------

class TestMacroVote(unittest.TestCase):

    def test_vote_reads_regime_tilt(self):
        from modules.harness import votes
        fake = {
            "ok": {"market": True},
            "regime": {"regime": "Goldilocks", "confidence": 45, "equity_tilt": 0.8,
                       "driver": "Copper/gold · Small caps", "shift_risk": "Moderate",
                       "playbook": "favor growth"},
            "leading": [{"label": "Volatility (VIX)", "state": "COMPLACENT", "meaning": "low"}],
            "macro": [], "leading_summary": "ok", "macro_summary": "ok", "health": {},
        }
        with mock.patch("modules.macro.build_dashboard", return_value=fake):
            v = votes.vote_macro()
        self.assertTrue(v["ok"])
        self.assertEqual(v["direction"], 1)
        self.assertGreater(v["conviction"], 0)
        self.assertEqual(v["detail"]["regime"]["regime"], "Goldilocks")

    def test_vote_fail_soft(self):
        from modules.harness import votes
        with mock.patch("modules.macro.build_dashboard", side_effect=RuntimeError("boom")):
            v = votes.vote_macro()
        self.assertFalse(v["ok"])
        self.assertEqual(v["direction"], 0)


if __name__ == "__main__":
    unittest.main()
