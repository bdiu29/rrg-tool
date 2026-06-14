"""
Unit tests for the theme tracker.

Covers the three load-bearing pieces: the equal-weight theme-index builder, the
DB-backed theme CRUD, and the end-to-end view assembly over an injected price
panel (mocked fetch — no network). The shared-math `close=` injection is what
lets a synthetic theme index flow through the sector ranking/RRG engines.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from modules import themes
from modules.themes import store as themes_store
from modules.rrg import signal


def _close_panel(n=400):
    """A/B beat SPY, C lags — over n business days."""
    i = np.arange(n)
    idx = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame({
        "A":   100 * np.exp(0.0006 * i),
        "B":   100 * np.exp(0.0005 * i),
        "C":   100 * np.exp(0.0001 * i),
        "SPY": 100 * np.exp(0.0003 * i),
    }, index=idx)


# ---------------------------------------------------------------------------
# Equal-weight theme index
# ---------------------------------------------------------------------------

class TestThemePanel(unittest.TestCase):
    def test_equal_weight_index_shape_and_order(self):
        close = _close_panel()
        defs = [{"id": 1, "name": "Strong", "symbols": ["A", "B"]},
                {"id": 2, "name": "Weak", "symbols": ["C"]}]
        panel, keys, name_map = themes._build_panel(defs, close, "SPY")
        self.assertEqual(set(keys), {"T1", "T2"})
        self.assertIn("SPY", panel.columns)
        self.assertEqual(name_map["T1"], "Strong")
        self.assertAlmostEqual(panel["T1"].dropna().iloc[0], 100.0, places=6)  # seeds at 100
        self.assertGreater(panel["T1"].dropna().iloc[-1],
                           panel["T2"].dropna().iloc[-1])                      # strong basket wins

    def test_handles_constituent_with_short_history(self):
        close = _close_panel()
        close.loc[close.index[:200], "B"] = np.nan          # B is born halfway in
        panel, _, _ = themes._build_panel(
            [{"id": 1, "name": "X", "symbols": ["A", "B"]}], close, "SPY")
        # A carries the early part (mean skips NaN); the index is defined throughout
        self.assertTrue(panel["T1"].notna().all())

    def test_unknown_tickers_skipped(self):
        close = _close_panel()
        panel, keys, _ = themes._build_panel(
            [{"id": 9, "name": "Ghost", "symbols": ["ZZZZ", "NOPE"]}], close, "SPY")
        self.assertEqual(keys, [])
        self.assertTrue(panel.empty)


# ---------------------------------------------------------------------------
# Theme store CRUD
# ---------------------------------------------------------------------------

class TestThemeStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (themes_store._DATA_DIR, themes_store.DB_PATH)
        themes_store._DATA_DIR = Path(self._tmp.name)
        themes_store.DB_PATH   = Path(self._tmp.name) / "themes.db"

    def tearDown(self):
        themes_store._DATA_DIR, themes_store.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_seed_is_idempotent(self):
        themes_store.init_db()
        first = themes_store.list_themes()
        self.assertEqual(len(first), len(themes_store.BUILTIN_THEMES))
        self.assertTrue(all(t["builtin"] for t in first))
        themes_store.seed_builtin_themes()                  # again → no dupes
        self.assertEqual(len(themes_store.list_themes()), len(first))

    def test_save_update_delete(self):
        themes_store.init_db()
        tid = themes_store.save_theme("My Theme", ["nvda", "amd", "nvda"], "desc")
        t = themes_store.get_theme(tid)
        self.assertEqual(t["name"], "My Theme")
        self.assertEqual(t["symbols"], ["AMD", "NVDA"])     # upper, dedup, sorted
        self.assertFalse(t["builtin"])

        themes_store.save_theme("My Theme 2", ["TSM"], "", theme_id=tid)  # update replaces wholesale
        t = themes_store.get_theme(tid)
        self.assertEqual(t["name"], "My Theme 2")
        self.assertEqual(t["symbols"], ["TSM"])

        themes_store.delete_theme(tid)
        self.assertIsNone(themes_store.get_theme(tid))

    def test_save_requires_name(self):
        themes_store.init_db()
        with self.assertRaises(ValueError):
            themes_store.save_theme("   ", ["AAPL"])


# ---------------------------------------------------------------------------
# End-to-end view over an injected (mocked) price panel
# ---------------------------------------------------------------------------

class TestThemeView(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (themes_store._DATA_DIR, themes_store.DB_PATH)
        themes_store._DATA_DIR = Path(self._tmp.name)
        themes_store.DB_PATH   = Path(self._tmp.name) / "themes.db"
        with themes_store.connect() as c:
            c.executescript(themes_store._SCHEMA)           # tables only, no seeds
        self.strong = themes_store.save_theme("Strong", ["A", "B"])
        self.weak   = themes_store.save_theme("Weak", ["C"])

    def tearDown(self):
        themes_store._DATA_DIR, themes_store.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_view_ranks_and_builds_rrg_and_leaders(self):
        with mock.patch.object(signal, "_fetch_close", return_value=_close_panel()):
            v = themes.compute_theme_view("daily", 6)
        names = [s["name"] for s in v["ranking"]["sectors"]]
        self.assertEqual(names[0], "Strong")                # strong basket ranks first
        self.assertIn("Weak", names)
        # RRG: one entry per theme, names mapped off the synthetic keys
        self.assertEqual(len(v["rrg"]["sectors"]), 2)
        self.assertTrue(all(d["name"] in ("Strong", "Weak")
                            for d in v["rrg"]["sectors"].values()))
        # constituent leaders keyed by theme id, ranked by RS
        strong_leaders = v["leaders"][str(self.strong)]
        self.assertEqual({r["symbol"] for r in strong_leaders}, {"A", "B"})
        self.assertGreaterEqual(strong_leaders[0]["rs_1m"], strong_leaders[-1]["rs_1m"])


if __name__ == "__main__":
    unittest.main()
