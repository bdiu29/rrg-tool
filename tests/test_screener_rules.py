"""
Unit tests for screener alert rules, armed-screen logic, and store-level
alert dedupe / channel routing (against a temp database).

Run:  /usr/bin/python3 -m unittest discover tests
"""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from modules.screener import rules, store


def _keys(alerts):
    return {a["rule_key"] for a in alerts}


class TestEvaluateRules(unittest.TestCase):
    def test_vol_thrust_boundaries(self):
        fire = rules.evaluate_rules({"chg_pct": 3.0, "rvol_10d": 3.0, "close": 10})
        self.assertIn("vol_thrust_up", _keys(fire))
        self.assertEqual(fire[0]["kind"], "pump")
        quiet = rules.evaluate_rules({"chg_pct": 3.0, "rvol_10d": 2.9, "close": 10})
        self.assertNotIn("vol_thrust_up", _keys(quiet))
        down = rules.evaluate_rules({"chg_pct": -3.0, "rvol_10d": 4.0, "close": 10})
        self.assertIn("vol_thrust_down", _keys(down))
        self.assertEqual(down[0]["kind"], "dump")

    def test_rsi_extremes(self):
        self.assertIn("rsi_overbought", _keys(rules.evaluate_rules({"rsi14": 80.0})))
        self.assertIn("rsi_oversold", _keys(rules.evaluate_rules({"rsi14": 20.0})))
        self.assertEqual(rules.evaluate_rules({"rsi14": 79.9}), [])
        self.assertEqual(rules.evaluate_rules({"rsi14": 50.0}), [])

    def test_ma_stretch(self):
        self.assertIn("ma_stretch_up",
                      _keys(rules.evaluate_rules({"price_vs_sma20_pct": 15.0})))
        self.assertIn("ma_stretch_down",
                      _keys(rules.evaluate_rules({"price_vs_sma20_pct": -15.0})))
        self.assertEqual(rules.evaluate_rules({"price_vs_sma20_pct": 14.9}), [])

    def test_breakout_levels_and_52w_suppression(self):
        base = {"close": 12.0, "high_20d": 10.0, "low_20d": 8.0,
                "high_252": 11.0, "low_252": 7.0}
        keys = _keys(rules.evaluate_rules(base))
        # 12 > both highs → only the 52w alert fires, the 20d echo is suppressed
        self.assertIn("break_52w_high", keys)
        self.assertNotIn("break_20d_high", keys)
        keys = _keys(rules.evaluate_rules({**base, "close": 10.5}))
        self.assertEqual(keys, {"break_20d_high"})
        keys = _keys(rules.evaluate_rules({**base, "close": 6.5}))
        self.assertIn("break_52w_low", keys)
        self.assertNotIn("break_20d_low", keys)

    def test_gaps(self):
        self.assertIn("gap_up", _keys(rules.evaluate_rules({"gap_pct": 4.0})))
        self.assertIn("gap_down", _keys(rules.evaluate_rules({"gap_pct": -4.0})))
        self.assertEqual(rules.evaluate_rules({"gap_pct": 3.9}), [])

    def test_earnings_soon(self):
        self.assertIn("earnings_soon", _keys(rules.evaluate_rules({"days_to_earnings": 7})))
        self.assertIn("earnings_soon", _keys(rules.evaluate_rules({"days_to_earnings": 0})))
        self.assertEqual(rules.evaluate_rules({"days_to_earnings": 8}), [])
        self.assertEqual(rules.evaluate_rules({"days_to_earnings": -1}), [])

    def test_nan_and_missing_fields_are_safe(self):
        self.assertEqual(rules.evaluate_rules({}), [])
        self.assertEqual(rules.evaluate_rules(
            {"chg_pct": float("nan"), "rvol_10d": float("nan"),
             "rsi14": None, "close": float("nan")}), [])


class TestArmedScreens(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame({
            "chg_pct": [5.0, 1.0],
            "volume":  [500_000.0, 500_000.0],
        }, index=["AAA", "BBB"])
        self.screen = {"id": 7, "name": "Movers",
                       "conditions": [{"field": "chg_pct", "op": ">", "value": 2}]}

    def test_fires_only_on_new_matches(self):
        alerts, state = rules.evaluate_armed_screens(
            self.df, ["AAA", "BBB"], [self.screen], {})
        self.assertEqual([a["symbol"] for a in alerts], ["AAA"])
        self.assertEqual(alerts[0]["rule_key"], "screen:7")
        self.assertEqual(state, {7: {"AAA"}})
        # same matches next evaluation → silence
        alerts2, _ = rules.evaluate_armed_screens(
            self.df, ["AAA", "BBB"], [self.screen], state)
        self.assertEqual(alerts2, [])

    def test_refires_after_symbol_leaves_and_returns(self):
        df_quiet = self.df.copy()
        df_quiet.loc["AAA", "chg_pct"] = 0.0
        _, state = rules.evaluate_armed_screens(
            df_quiet, ["AAA", "BBB"], [self.screen], {7: {"AAA"}})
        self.assertEqual(state, {7: set()})
        alerts, _ = rules.evaluate_armed_screens(
            self.df, ["AAA", "BBB"], [self.screen], state)
        self.assertEqual([a["symbol"] for a in alerts], ["AAA"])

    def test_focus_list_scopes_evaluation(self):
        alerts, state = rules.evaluate_armed_screens(
            self.df, ["BBB"], [self.screen], {})
        self.assertEqual(alerts, [])
        self.assertEqual(state, {7: set()})


class TestStoreAlertsAndRouting(unittest.TestCase):
    """Dedupe + channel routing against a temp screener.db."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = store.DB_PATH
        store.DB_PATH = Path(self._tmp.name) / "screener-test.db"
        store.init_db()

    def tearDown(self):
        store.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_alert_dedupe_per_day(self):
        first = store.insert_alert("2026-06-11", "NVDA", "vol_thrust_up",
                                   "pump", "+5% on 4x vol")
        again = store.insert_alert("2026-06-11", "NVDA", "vol_thrust_up",
                                   "pump", "+6% on 5x vol")
        self.assertTrue(first)
        self.assertFalse(again)
        # new day or different rule → fresh alert
        self.assertTrue(store.insert_alert("2026-06-12", "NVDA", "vol_thrust_up",
                                           "pump", "again"))
        self.assertTrue(store.insert_alert("2026-06-11", "NVDA", "gap_up",
                                           "pump", "gap"))

    def test_channel_routing_unions_watchlists(self):
        store.save_watchlist("discord list", ["AAA", "BBB"], channels=["discord"])
        store.save_watchlist("email list", ["BBB"], channels=["email"])
        store.save_watchlist("quiet list", ["CCC"], channels=[])
        routes = store.channels_for_symbols(["AAA", "BBB", "CCC", "ZZZ"])
        self.assertEqual(routes["AAA"], {"discord"})
        self.assertEqual(routes["BBB"], {"discord", "email"})
        self.assertEqual(routes["CCC"], set())
        self.assertEqual(routes["ZZZ"], set())

    def test_positions_channels_meta(self):
        self.assertEqual(store.get_positions_channels(), set())
        store.set_positions_channels(["discord"])
        self.assertEqual(store.get_positions_channels(), {"discord"})

    def test_seed_builtins_idempotent(self):
        store.seed_builtin_screens()
        store.seed_builtin_screens()
        names = [s["name"] for s in store.list_screens()]
        self.assertEqual(sorted(names), ["Breakout", "Continuation"])

    def test_screen_match_memory(self):
        store.update_screen_matches(1, {"AAA", "BBB"}, "2026-06-11")
        self.assertEqual(store.get_screen_matches(1), {"AAA", "BBB"})
        store.update_screen_matches(1, {"BBB"}, "2026-06-12")
        self.assertEqual(store.get_screen_matches(1), {"BBB"})


if __name__ == "__main__":
    unittest.main()
