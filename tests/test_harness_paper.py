"""Unit tests for the paper-trading engine (Phase 3 / Layer B). Hermetic: a temp DB,
injected prices/decision/suggestions — no network. Covers target_book (pure), the
idempotent step, both books incl. the SPY hedge leg, stop-outs, mark-to-market, state."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from modules.harness import paper, store


def _sugg(sym, pick, stop, tradeable=True):
    return {"symbol": sym, "pick": pick, "stop": stop, "tradeable": tradeable}


class TestTargetBook(unittest.TestCase):
    def setUp(self):
        self.sugg = [_sugg("AAA", 90, 80), _sugg("BBB", 80, 70), _sugg("CCC", 70, 60),
                     _sugg("DDD", 60, 50), _sugg("EEE", 50, 40),
                     _sugg("JUNK", 99, 1, tradeable=False)]

    def test_concentrate_takes_top3_gross_full(self):
        tb = paper.target_book(self.sugg, "CONCENTRATE", "Risk-on")
        self.assertEqual(set(tb["long"]), {"AAA", "BBB", "CCC"})       # top 3 by pick
        self.assertAlmostEqual(sum(tb["long"].values()), 1.0)         # Risk-on gross = 1.0
        self.assertGreater(tb["long"]["AAA"], tb["long"]["CCC"])      # weight ∝ pick
        self.assertNotIn("JUNK", tb["long"])                          # non-tradeable excluded

    def test_rotate_broadens(self):
        tb = paper.target_book(self.sugg, "ROTATE", "Risk-on")
        self.assertEqual(len(tb["long"]), 5)                          # n=6 but only 5 tradeable

    def test_posture_scales_gross(self):
        tb = paper.target_book(self.sugg, "ROTATE", "Lean bullish")
        self.assertAlmostEqual(sum(tb["long"].values()), 0.75)

    def test_risk_off_is_flat(self):
        tb = paper.target_book(self.sugg, "CONCENTRATE", "Risk-off")
        self.assertEqual(tb["long"], {})
        self.assertEqual(tb["gross"], 0.0)


class _PaperTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self._orig = (store.DB_PATH, store._DATA_DIR)
        store.DB_PATH = d / "trading.db"
        store._DATA_DIR = d
        store.init_db()

    def tearDown(self):
        store.DB_PATH, store._DATA_DIR = self._orig
        self._tmp.cleanup()

    DECISION = {"stance": "ROTATE", "posture": "Risk-on", "score": 50}


class TestStep(_PaperTestBase):
    def test_buys_both_books_with_hedge(self):
        sugg = [_sugg("AAA", 80, 90), _sugg("BBB", 60, 45)]
        prices = {"AAA": 100.0, "BBB": 50.0, "SPY": 400.0, "RSP": 150.0}
        res = paper.step(asof="2026-06-15", prices=prices, decision=self.DECISION,
                         suggestions=sugg)
        self.assertFalse(res["skipped"])

        lo = store.get_positions("long_only")
        self.assertEqual(set(lo), {"AAA", "BBB"})
        self.assertGreater(lo["AAA"]["shares"], 0)
        self.assertEqual(lo["AAA"]["stop"], 90)

        hg = store.get_positions("hedged")
        self.assertIn("SPY", hg)
        self.assertLess(hg["SPY"]["shares"], 0)                       # short the benchmark
        # net market exposure ≈ 0 for the hedged book (long ≈ short SPY notional)
        net = sum(p["shares"] * prices[s] for s, p in hg.items())
        self.assertLess(abs(net), 0.02 * paper.STARTING_CAPITAL)
        # a cost was paid (slippage on the traded notional)
        self.assertGreater(store.get_steps("long_only")[0]["cost_paid"], 0)

    def test_idempotent_same_day(self):
        sugg = [_sugg("AAA", 80, 90)]
        prices = {"AAA": 100.0, "SPY": 400.0, "RSP": 150.0}
        paper.step(asof="2026-06-15", prices=prices, decision=self.DECISION, suggestions=sugg)
        again = paper.step(asof="2026-06-15", prices=prices, decision=self.DECISION,
                           suggestions=sugg)
        self.assertTrue(again["skipped"])
        self.assertEqual(len(store.get_steps("long_only")), 1)        # not double-counted

    def test_stop_out_exits(self):
        sugg = [_sugg("AAA", 80, 90)]
        paper.step(asof="2026-06-15", prices={"AAA": 100.0, "SPY": 400.0, "RSP": 150.0},
                   decision=self.DECISION, suggestions=sugg)
        # next day AAA falls below its 90 stop, and it's no longer a suggestion
        paper.step(asof="2026-06-16", prices={"AAA": 85.0, "SPY": 400.0, "RSP": 150.0},
                   decision=self.DECISION, suggestions=[])
        self.assertNotIn("AAA", store.get_positions("long_only"))
        stops = [f for f in store.get_fills("long_only") if f["reason"] == "stop"]
        self.assertTrue(stops)

    def test_catch_up_backfills_missed_days(self):
        import pandas as pd
        paper.step(asof="2026-06-15", prices={"AAA": 100.0, "SPY": 400.0, "RSP": 150.0},
                   decision=self.DECISION, suggestions=[_sugg("AAA", 80, 50)])
        idx = pd.to_datetime(["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18"])
        panel = pd.DataFrame({"AAA": [100.0, 110.0, 120.0, 130.0],
                              "SPY": [400.0, 402.0, 404.0, 406.0],
                              "RSP": [150.0, 151.0, 152.0, 153.0]}, index=idx)
        with mock.patch("modules.rrg.signal._fetch_close", return_value=panel):
            n = paper.catch_up(today="2026-06-18")          # reopened after a 2-day gap
        self.assertEqual(n, 4)                               # 06-16 + 06-17, both books
        lo = {s["date"]: s for s in store.get_steps("long_only")}
        self.assertIn("2026-06-16", lo)
        self.assertIn("2026-06-17", lo)
        self.assertGreater(lo["2026-06-17"]["equity"], lo["2026-06-16"]["equity"])  # AAA ↑
        self.assertEqual(lo["2026-06-16"]["turnover"], 0.0)  # marks only, no trades
        self.assertEqual(lo["2026-06-16"]["spy"], 402.0)     # benchmark marked point-in-time

    def test_catch_up_noop_without_prior_steps(self):
        self.assertEqual(paper.catch_up(today="2026-06-18"), 0)   # nothing held yet

    def test_mark_to_market(self):
        sugg = [_sugg("AAA", 80, 50)]
        paper.step(asof="2026-06-15", prices={"AAA": 100.0, "SPY": 400.0, "RSP": 150.0},
                   decision=self.DECISION, suggestions=sugg)
        eq1 = store.get_steps("long_only")[-1]["equity"]
        # next day AAA +20% (still held, still suggested, well above its stop)
        paper.step(asof="2026-06-16", prices={"AAA": 120.0, "SPY": 400.0, "RSP": 150.0},
                   decision=self.DECISION, suggestions=sugg)
        eq2 = store.get_steps("long_only")[-1]["equity"]
        self.assertGreater(eq2, eq1)


class TestState(_PaperTestBase):
    def test_state_assembles_with_gate(self):
        sugg = [_sugg("AAA", 80, 50)]
        paper.step(asof="2026-06-15", prices={"AAA": 100.0, "SPY": 400.0, "RSP": 150.0},
                   decision=self.DECISION, suggestions=sugg)
        paper.step(asof="2026-06-16", prices={"AAA": 110.0, "SPY": 404.0, "RSP": 151.0},
                   decision=self.DECISION, suggestions=sugg)
        st = paper.state()
        self.assertIn("long_only", st["books"])
        self.assertIn("hedged", st["books"])
        self.assertEqual(st["books"]["long_only"]["days"], 2)
        self.assertIsNotNone(st["books"]["long_only"]["total_return"])
        self.assertIn("hedged_total_return", st["gate"])


if __name__ == "__main__":
    unittest.main()
