"""
Unit tests for the screener's JSON condition engine.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest

import numpy as np
import pandas as pd

from modules.screener import filters
from modules.screener.store import BUILTIN_SCREENS


def _frame():
    return pd.DataFrame({
        "close":       [10.0, 50.0, 100.0, 200.0],
        "chg_pct":     [5.0, -3.0, 0.5, np.nan],
        "volume":      [500_000, 50_000, 2_000_000, 150_000],
        "vol_chg_pct": [120.0, 10.0, 95.0, np.nan],
        "rvol_10d":    [2.5, 0.8, 1.4, np.nan],
        "market_cap":  [5e8, 5e6, 2e10, np.nan],
        "sma50":       [9.0, 55.0, 90.0, 210.0],
        "sma150":      [8.0, 60.0, 80.0, 220.0],
        "sector_etf":  ["XLK", "XLE", None, "XLK"],
        "earnings_date": [None, "2026-06-15", "2026-07-30", None],
    }, index=["UP", "DOWN", "BIG", "GAPPY"])


class TestOps(unittest.TestCase):
    def setUp(self):
        self.df = filters.derive_scan_columns(_frame(), today="2026-06-11")

    def _run(self, field, op, value):
        return set(filters.apply_filters(
            self.df, [{"field": field, "op": op, "value": value}]).index)

    def test_comparison_ops(self):
        self.assertEqual(self._run("chg_pct", ">", 1), {"UP"})
        self.assertEqual(self._run("chg_pct", ">=", 0.5), {"UP", "BIG"})
        self.assertEqual(self._run("chg_pct", "<", 0), {"DOWN"})
        self.assertEqual(self._run("chg_pct", "<=", -3), {"DOWN"})
        self.assertEqual(self._run("volume", "==", 50_000), {"DOWN"})

    def test_between_and_in(self):
        self.assertEqual(self._run("market_cap", "between", [1e8, 1e11]),
                         {"UP", "BIG"})
        self.assertEqual(self._run("sector_etf", "in", ["XLK"]), {"UP", "GAPPY"})

    def test_nan_never_matches(self):
        # GAPPY has NaN chg_pct — `!=` must NOT let it through
        self.assertNotIn("GAPPY", self._run("chg_pct", "!=", 99))
        self.assertNotIn("GAPPY", self._run("market_cap", "<", 1e20))
        # BIG has None sector_etf
        self.assertNotIn("BIG", self._run("sector_etf", "!=", "XLE"))

    def test_conditions_and_together(self):
        got = filters.apply_filters(self.df, [
            {"field": "chg_pct", "op": ">", "value": 1},     # excludes BIG's 0.5
            {"field": "volume",  "op": ">", "value": 100_000},
        ])
        self.assertEqual(set(got.index), {"UP"})

    def test_unknown_field_matches_nothing(self):
        self.assertEqual(self._run("bogus", ">", 0), set())

    def test_empty_conditions_returns_all(self):
        self.assertEqual(len(filters.apply_filters(self.df, [])), len(self.df))


class TestDerivedColumns(unittest.TestCase):
    def test_price_vs_sma(self):
        df = filters.derive_scan_columns(_frame())
        # UP: close 10 vs sma50 9 → +11.1%
        self.assertAlmostEqual(df.loc["UP", "price_vs_sma50_pct"], 100 / 9, places=4)
        # DOWN: close 50 vs sma50 55 → negative
        self.assertLess(df.loc["DOWN", "price_vs_sma50_pct"], 0)

    def test_days_to_earnings(self):
        df = filters.derive_scan_columns(_frame(), today="2026-06-11")
        self.assertEqual(df.loc["DOWN", "days_to_earnings"], 4)
        self.assertTrue(np.isnan(df.loc["UP", "days_to_earnings"]))


class TestValidation(unittest.TestCase):
    def test_valid_conditions_pass(self):
        conds = [{"field": "chg_pct", "op": ">", "value": 2},
                 {"field": "market_cap", "op": "between", "value": [1e7, 1e13]},
                 {"field": "sector_etf", "op": "in", "value": ["XLK"]}]
        self.assertEqual(filters.validate_conditions(conds), [])

    def test_bad_conditions_reported(self):
        self.assertTrue(filters.validate_conditions("nope"))
        self.assertTrue(filters.validate_conditions([{"field": "bogus", "op": ">", "value": 1}]))
        self.assertTrue(filters.validate_conditions([{"field": "sector_etf", "op": ">", "value": 1}]))
        self.assertTrue(filters.validate_conditions([{"field": "chg_pct", "op": "between", "value": [1]}]))
        self.assertTrue(filters.validate_conditions([{"field": "chg_pct", "op": ">", "value": "x"}]))


class TestBuiltinScreens(unittest.TestCase):
    """The user's real TradingView presets must parse and behave."""

    def test_presets_validate(self):
        for spec in BUILTIN_SCREENS:
            self.assertEqual(filters.validate_conditions(spec["conditions"]), [],
                             msg=spec["name"])

    def test_breakout_preset_selects_expected_row(self):
        # UP: cap 5e8 in range, vol_chg 120>80, vol 500k>100k,
        #     close 10 ≥ sma50 9, rvol 2.5>1 → matches.
        # DOWN fails most legs; BIG fails vol_chg... actually 95>80, but
        # rvol 1.4>1 and close 100 ≥ sma50 90 → BIG matches too.
        df = filters.derive_scan_columns(_frame())
        breakout = next(s for s in BUILTIN_SCREENS if s["name"] == "Breakout")
        got = set(filters.apply_filters(df, breakout["conditions"]).index)
        self.assertEqual(got, {"UP", "BIG"})

    def test_continuation_preset_requires_chg(self):
        df = filters.derive_scan_columns(_frame())
        cont = next(s for s in BUILTIN_SCREENS if s["name"] == "Continuation")
        got = set(filters.apply_filters(df, cont["conditions"]).index)
        self.assertEqual(got, {"UP"})   # BIG's chg 0.5% < 2%


if __name__ == "__main__":
    unittest.main()
