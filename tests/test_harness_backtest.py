"""Unit tests for the harness referee (Phase 2) — the per-sector confluence scoring
(single source of truth), the confluence call-panel derivation, the A/B summary, and
an end-to-end assembly over a synthetic price panel (no network — bt._load mocked)."""

import unittest
from unittest import mock

import numpy as np
import pandas as pd

from modules.harness import combiner
from modules.harness import backtest as hb


# ---------------------------------------------------------------------------
# combiner.score_sectors — the regime-arbitrated per-sector confluence
# ---------------------------------------------------------------------------

class TestScoreSectors(unittest.TestCase):
    def _rows(self):
        return [{"ticker": "XLK", "name": "Tech", "call": "ROTATE IN", "conviction": 60}]

    def test_agreement_bonus_in_rotate(self):
        # ROTATE stance (no suppression): (60 + (80-50)*0.6) * 1.25 agree = 97.5
        s = combiner.score_sectors(self._rows(), {"XLK": 80}, stance="ROTATE")[0]
        self.assertAlmostEqual(s["score"], 97.5)
        self.assertTrue(s["agree"])

    def test_concentrate_halves_the_rrg_component(self):
        # CONCENTRATE halves the RRG conviction: (60*0.5 + 18) * 1.25 = 60.0 < ROTATE
        rot = combiner.score_sectors(self._rows(), {"XLK": 80}, stance="ROTATE")[0]["score"]
        con = combiner.score_sectors(self._rows(), {"XLK": 80}, stance="CONCENTRATE")[0]["score"]
        self.assertAlmostEqual(con, 60.0)
        self.assertLess(con, rot)

    def test_none_stance_matches_rotate(self):
        # live default (stance=None) applies no factor → same number as ROTATE
        none = combiner.score_sectors(self._rows(), {"XLK": 80}, stance=None)[0]["score"]
        rot  = combiner.score_sectors(self._rows(), {"XLK": 80}, stance="ROTATE")[0]["score"]
        self.assertAlmostEqual(none, rot)

    def test_concentrate_can_drop_a_marginal_long_below_threshold(self):
        rows = [{"ticker": "XLF", "name": "Fin", "call": "ROTATE IN", "conviction": 40}]
        rot = combiner.score_sectors(rows, {"XLF": 55}, stance="ROTATE")[0]["score"]
        con = combiner.score_sectors(rows, {"XLF": 55}, stance="CONCENTRATE")[0]["score"]
        self.assertGreaterEqual(rot, hb.T_LONG)         # a long when rotation is live
        self.assertLess(con, hb.T_LONG)                 # suppressed out of the long set when narrow

    def test_no_rank_is_handled(self):
        s = combiner.score_sectors(self._rows(), {}, stance="ROTATE")[0]
        self.assertEqual(s["score"], 60.0)              # RRG conviction only, no tilt
        self.assertFalse(s["agree"])
        self.assertIsNone(s["rank"])


# ---------------------------------------------------------------------------
# build_harness_calls — the confluence call panel (replay_calls shape)
# ---------------------------------------------------------------------------

class TestBuildHarnessCalls(unittest.TestCase):
    def setUp(self):
        self.d = pd.Timestamp("2026-06-15")
        self.rrg_calls = {
            "XLK": {self.d: {"call": "ROTATE IN", "conviction": 60}},
            "XLU": {self.d: {"call": "ROTATE OUT", "conviction": 50}},
        }
        self.rank = pd.DataFrame({"XLK": [80.0], "XLU": [20.0]}, index=[self.d])
        self.regime = pd.Series({self.d: "HEALTHY"})
        self.rotation = pd.Series({self.d: "on"})        # → ROTATE stance

    def test_maps_scores_to_calls(self):
        out = hb.build_harness_calls(self.rrg_calls, self.rank, self.regime, self.rotation)
        self.assertEqual(out["XLK"][self.d]["call"], "ROTATE IN")     # score 97.5
        self.assertEqual(out["XLU"][self.d]["call"], "ROTATE OUT")    # score -85
        self.assertGreater(out["XLK"][self.d]["conviction"], 60)
        self.assertLessEqual(out["XLK"][self.d]["conviction"], 99)

    def test_concentrate_demotes_a_marginal_long(self):
        rrg = {"XLF": {self.d: {"call": "ROTATE IN", "conviction": 40}}}
        rank = pd.DataFrame({"XLF": [55.0]}, index=[self.d])
        on  = hb.build_harness_calls(rrg, rank, pd.Series({self.d: "HEALTHY"}),
                                     pd.Series({self.d: "on"}))
        off = hb.build_harness_calls(rrg, rank, pd.Series({self.d: "DETERIORATING"}),
                                     pd.Series({self.d: "off"}))
        self.assertEqual(on["XLF"][self.d]["call"], "ROTATE IN")
        self.assertNotEqual(off["XLF"][self.d]["call"], "ROTATE IN")  # suppressed → HOLD/WATCH

    def test_works_without_rank_or_regime(self):
        out = hb.build_harness_calls(self.rrg_calls, None, None, None)
        self.assertIn(out["XLK"][self.d]["call"], ("ROTATE IN", "HOLD"))


class TestScoreToCall(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(hb._score_to_call(hb.T_LONG + 1), "ROTATE IN")
        self.assertEqual(hb._score_to_call(-hb.T_OUT - 1), "ROTATE OUT")
        self.assertEqual(hb._score_to_call(5), "HOLD")
        self.assertEqual(hb._score_to_call(-5), "WATCH")


# ---------------------------------------------------------------------------
# A/B summary + verdict (pure, hand-built panel reports)
# ---------------------------------------------------------------------------

class TestABSummary(unittest.TestCase):
    def _eq(self, ret, dd):
        return {"total_return": ret, "max_drawdown": dd, "sharpe": 1.0,
                "time_in_market": 50.0, "bench_matched_return": 10.0,
                "bench_total_return": 30.0}

    def test_verdict_beats_both(self):
        ab = hb._ab_summary({"equity": self._eq(40, -8), "event_study": {}, "rotation_portfolio": None},
                            {"equity": self._eq(20, -10), "event_study": {}, "rotation_portfolio": None})
        self.assertIn("beat BOTH", ab["verdict"])
        self.assertEqual(ab["long_only"]["harness"]["total_return"], 40)

    def test_verdict_null_result(self):
        ab = hb._ab_summary({"equity": self._eq(5, -12), "event_study": {}, "rotation_portfolio": None},
                            {"equity": self._eq(20, -10), "event_study": {}, "rotation_portfolio": None})
        self.assertIn("did NOT beat beta", ab["verdict"])   # 5% < 10% matched

    def test_verdict_no_trades(self):
        ab = hb._ab_summary({"equity": None, "event_study": {}, "rotation_portfolio": None},
                            {"equity": None, "event_study": {}, "rotation_portfolio": None})
        self.assertIn("No harness trades", ab["verdict"])


# ---------------------------------------------------------------------------
# End-to-end assembly over a synthetic panel (no network)
# ---------------------------------------------------------------------------

def _synthetic_load(_interval, _params, _tickers):
    """Build (series, ohlc, idx, close, spy_close, spy_arr) from a deterministic
    synthetic price panel — mirrors how themes injects a close panel, so no yfinance."""
    from modules.rrg import signal
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2024-01-01", periods=320)
    tickers = ["XLK", "XLF", "XLU", "SPY", "RSP"]
    cols = {}
    for i, t in enumerate(tickers):
        drift = 0.0006 + 0.0002 * i
        steps = rng.normal(drift, 0.01, len(idx))
        cols[t] = 100 * np.exp(np.cumsum(steps))
    close = pd.DataFrame(cols, index=idx)
    series, _, close = signal.compute_series(["XLK", "XLF", "XLU"], "SPY", "1d", close=close)
    ohlc = {"open": close.shift(1).fillna(close), "high": close * 1.01,
            "low": close * 0.99, "close": close}
    from modules.rrg import backtest as bt
    spy_close, spy_arr = bt._bench(close, close.index, "SPY")
    return series, ohlc, close.index, close, spy_close, spy_arr


class TestRunHarnessBacktestE2E(unittest.TestCase):
    def test_report_assembles_without_network(self):
        with mock.patch("modules.rrg.backtest._load", side_effect=_synthetic_load), \
             mock.patch("modules.harness.backtest._breadth_regime_panel", return_value=None):
            rep = hb.run_harness_backtest(interval="1d")
        self.assertNotIn("error", rep)
        for key in ("harness", "rrg", "ab", "caveats", "config"):
            self.assertIn(key, rep)
        self.assertIsInstance(rep["ab"]["verdict"], str)
        self.assertIn("harness", rep["ab"]["long_only"])
        self.assertIsInstance(rep["harness"]["event_study"], dict)
        # the text formatter must not throw on whatever shape came back
        self.assertIn("HARNESS REFEREE", hb.format_report(rep))


if __name__ == "__main__":
    unittest.main()
