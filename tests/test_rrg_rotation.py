"""
Unit tests for the cross-sectional long/short rotation portfolio (the top-N sim).

Four deterministic tickers with constant daily returns and fixed calls/conviction
let us assert exactly: top-N selection ranks by CONVICTION not return, the leg
P&L decomposition (long mean − short mean), no-lookahead (weights earn the bar
AFTER the call), and the rotation-off gate flattening the book.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest

import numpy as np
import pandas as pd

from modules.rrg import backtest


N = 12
IDX = pd.bdate_range("2026-01-02", periods=N)

# Four names, each a constant-daily-return ramp so the leg returns are exact.
# Conviction ranks A>B>C among the ROTATE INs; C has the HIGHEST raw return but
# the LOWEST conviction — so a conviction-ranked top-2 long leg must drop it.
SPECS = {
    "A": {"call": "ROTATE IN",  "conv": 90.0, "g": 1.02},
    "B": {"call": "ROTATE IN",  "conv": 60.0, "g": 1.01},
    "C": {"call": "ROTATE IN",  "conv": 30.0, "g": 1.05},
    "D": {"call": "ROTATE OUT", "conv": -40.0, "g": 0.99},
}


def _close():
    return pd.DataFrame(
        {tk: [100.0 * s["g"] ** i for i in range(N)] for tk, s in SPECS.items()},
        index=IDX)


def _calls():
    out = {}
    for tk, s in SPECS.items():
        out[tk] = {d: {"call": s["call"], "conviction": s["conv"],
                       "quadrant": "", "phase": ""} for d in IDX}
    return out


def _bench():
    return pd.Series(100.0, index=IDX)         # flat SPY → matched curve stays ~1


def _cfg(**kw):
    return backtest._portfolio_cfg({"n_long": 2, "n_short": 1, **kw})


def _run(rotation=None, bench=None, **kw):
    return backtest._rotation_portfolio(_calls(), _close(),
                                        _bench() if bench is None else bench, IDX,
                                        _cfg(**kw), rotation)


class TestLegSelection(unittest.TestCase):
    def test_long_leg_is_top_n_by_conviction_not_return(self):
        call_df, conv_df = backtest._state_panels(_calls(), IDX)
        long_w, short_w = backtest._leg_weights(call_df, conv_df, _cfg())
        last = long_w.iloc[-1]
        self.assertEqual(set(last[last > 0].index), {"A", "B"})   # C dropped despite +5%/day
        self.assertAlmostEqual(last["A"], 0.5, places=9)
        self.assertAlmostEqual(last["B"], 0.5, places=9)
        sh = short_w.iloc[-1]
        self.assertEqual(set(sh[sh > 0].index), {"D"})
        self.assertAlmostEqual(sh["D"], 1.0, places=9)

    def test_state_panels_dense_and_ffilled(self):
        call_df, conv_df = backtest._state_panels(_calls(), IDX)
        self.assertEqual(list(call_df.columns), list(SPECS))
        self.assertEqual(call_df.loc[IDX[-1], "A"], "ROTATE IN")
        self.assertAlmostEqual(conv_df.loc[IDX[-1], "D"], -40.0, places=6)


class TestLegPnLAndNoLookahead(unittest.TestCase):
    def test_decomposition_and_shift(self):
        rep = _run()
        # window starts one bar in (shifted weights ⇒ bar 0 is flat) → 11 marked bars
        self.assertEqual(len(rep["dates"]), N - 1)
        m = N - 1
        long_daily  = 0.5 * 0.02 + 0.5 * 0.01            # A,B equal-weight
        short_daily = -0.01                              # D
        spread_daily = long_daily - short_daily
        self.assertAlmostEqual(rep["long_return"],   (1 + long_daily) ** m * 100 - 100, places=4)
        self.assertAlmostEqual(rep["short_return"],  (1 - short_daily) ** m * 100 - 100, places=4)
        self.assertAlmostEqual(rep["spread_return"], (1 + spread_daily) ** m * 100 - 100, places=4)
        # default mode is long_short ⇒ strategy == spread
        self.assertAlmostEqual(rep["total_return"], rep["spread_return"], places=6)
        self.assertEqual(rep["avg_n_long"], 2.0)
        self.assertEqual(rep["avg_n_short"], 1.0)
        self.assertAlmostEqual(rep["avg_turnover"], 0.0, places=6)  # constant membership

    def test_mode_long_only(self):
        rep = _run(mode="long_only")
        m = N - 1
        self.assertAlmostEqual(rep["total_return"], (1.015) ** m * 100 - 100, places=4)

    def test_mode_short_only(self):
        rep = _run(mode="short_only")
        m = N - 1
        self.assertAlmostEqual(rep["total_return"], (1.01) ** m * 100 - 100, places=4)

    def test_mode_long_hedged_subtracts_benchmark(self):
        # benchmark rises 0.5%/day; long book is the +1.5%/day top-2 leg, fully
        # invested every bar ⇒ hedged daily = 0.015 − 0.005 = 0.010.
        bench = pd.Series([100.0 * 1.005 ** i for i in range(N)], index=IDX)
        rep = _run(bench=bench, mode="long_hedged")
        m = N - 1
        self.assertAlmostEqual(rep["total_return"], (1.010) ** m * 100 - 100, places=4)
        self.assertAlmostEqual(rep["hedged_return"], rep["total_return"], places=6)
        # the hedge drags below the unhedged long-only leg (positive benchmark)
        self.assertLess(rep["hedged_return"], rep["long_return"])

    def test_long_hedged_equals_long_only_when_benchmark_flat(self):
        # flat benchmark (the default _bench) ⇒ no hedge drag ⇒ hedged == long-only
        rep = _run(mode="long_hedged")
        self.assertAlmostEqual(rep["hedged_return"], rep["long_return"], places=6)


class TestRotationGate(unittest.TestCase):
    def test_gate_off_flattens_to_none(self):
        rot = pd.Series("off", index=IDX)
        self.assertIsNone(_run(rotation=rot, gate=True))

    def test_gate_on_matches_ungated(self):
        rot = pd.Series("on", index=IDX)
        gated = _run(rotation=rot, gate=True)
        plain = _run(rotation=None)
        self.assertAlmostEqual(gated["total_return"], plain["total_return"], places=9)
        self.assertTrue(gated["gated"])

    def test_gate_disabled_ignores_regime(self):
        rot = pd.Series("off", index=IDX)
        rep = _run(rotation=rot, gate=False)        # off everywhere but gate disabled
        self.assertIsNotNone(rep)
        self.assertFalse(rep["gated"])


if __name__ == "__main__":
    unittest.main()
