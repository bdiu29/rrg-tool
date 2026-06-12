"""
Unit tests for the screener strategy backtester.

A single deterministic symbol with one engineered signal lets us assert exact
entry/exit prices (next-open execution, no lookahead) for each exit model, the
forward-return measurement, and the aggregate stats.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest
from unittest import mock

import numpy as np
import pandas as pd

from modules.screener import backtest


def _dates(n, start="2026-01-05"):
    return pd.bdate_range(start, periods=n).strftime("%Y-%m-%d")


def _panels():
    """AAA: flat at 100 for 40 bars, then a single +10% jump (the signal) on
    bar 40, drifting 112→113→114 over the next bars, then back to flat."""
    n = 60
    idx = _dates(n)
    c = [100.0] * 40 + [110.0, 112.0, 113.0, 114.0] + [100.0] * 16
    close = pd.DataFrame({"AAA": c}, index=idx)
    open_ = close.shift(1).fillna(100.0)
    open_.iloc[41, 0] = 110.0                      # entry bar open we assert against
    high  = close + 1.0
    low   = close - 1.0
    volume = pd.DataFrame({"AAA": [100_000.0] * n}, index=idx)
    return close, volume, open_, high, low, idx


class _Harness:
    """Patches the backtester's data dependencies onto small in-memory panels."""

    def __init__(self):
        self.close, self.volume, self.open_, self.high, self.low, self.idx = _panels()
        self.spy = pd.DataFrame({"close": np.linspace(50.0, 51.0, len(self.idx))},
                                index=self.idx)

    def __enter__(self):
        self._p = [
            mock.patch.object(backtest.breadth_universes, "load_config",
                              return_value={"universes": {"sp500": {}}}),
            mock.patch.object(backtest.breadth_store, "get_members",
                              return_value=["AAA"]),
            mock.patch.object(backtest.breadth_store, "get_panels",
                              return_value=(self.close, self.volume, self.open_,
                                            self.high, self.low)),
            mock.patch.object(backtest.breadth_store, "get_series",
                              return_value=self.spy),
            mock.patch.object(backtest.store, "get_fundamentals",
                              return_value=pd.DataFrame()),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()


COND = [{"field": "chg_pct", "op": ">", "value": 5}]   # fires once, on the +10% bar


class TestBacktestHold(unittest.TestCase):
    def test_one_trade_next_open_execution(self):
        with _Harness():
            rep = backtest.run_backtest(
                COND, universe="sp500",
                exit_cfg={"model": "hold", "hold_days": 3})
        self.assertEqual(rep["n_trades_total"], 1)
        t = rep["trades"][0]
        # signal on the jump bar (40); entry at the NEXT bar's open (no lookahead)
        self.assertLess(t["signal_date"], t["entry_date"])
        self.assertAlmostEqual(t["entry_px"], 110.0)
        # hold 3 bars → exit at close of entry+2 = 114
        self.assertEqual(t["bars_held"], 3)
        self.assertAlmostEqual(t["exit_px"], 114.0)
        self.assertAlmostEqual(t["return_pct"], (114.0 / 110.0 - 1) * 100, places=4)
        self.assertEqual(t["exit_reason"], "hold")

    def test_forward_returns_measured_from_entry_close(self):
        with _Harness():
            rep = backtest.run_backtest(COND, universe="sp500",
                                        exit_cfg={"model": "hold", "hold_days": 3})
        t = rep["trades"][0]
        # entry bar close 112 → +1 bar 113
        self.assertAlmostEqual(t["fwd"]["1"] if "1" in t["fwd"] else t["fwd"][1],
                               (113.0 / 112.0 - 1) * 100, places=4)

    def test_stats_present(self):
        with _Harness():
            rep = backtest.run_backtest(COND, universe="sp500",
                                        exit_cfg={"model": "hold", "hold_days": 3})
        s = rep["stats"]
        self.assertEqual(s["n_trades"], 1)
        self.assertEqual(s["win_rate"], 100.0)
        self.assertGreater(s["avg_return"], 0)


class TestBacktestSignalExit(unittest.TestCase):
    def test_exits_when_signal_gone(self):
        with _Harness():
            rep = backtest.run_backtest(COND, universe="sp500",
                                        exit_cfg={"model": "signal", "max_hold": 20})
        t = rep["trades"][0]
        # the signal (chg>5) is gone the bar after the jump → exit at that close (112)
        self.assertEqual(t["bars_held"], 1)
        self.assertAlmostEqual(t["exit_px"], 112.0)
        self.assertEqual(t["exit_reason"], "signal")


class TestBacktestAtr(unittest.TestCase):
    def test_atr_produces_a_trade(self):
        with _Harness():
            rep = backtest.run_backtest(
                COND, universe="sp500",
                exit_cfg={"model": "atr", "atr_stop": 1.0, "atr_target": 1.0,
                          "max_hold": 10})
        self.assertEqual(rep["n_trades_total"], 1)
        self.assertIn(rep["trades"][0]["exit_reason"], ("stop", "target", "cap"))


class TestBacktestGuards(unittest.TestCase):
    def test_no_signal_no_trades(self):
        with _Harness():
            rep = backtest.run_backtest(
                [{"field": "chg_pct", "op": ">", "value": 999}], universe="sp500")
        self.assertEqual(rep["n_trades_total"], 0)
        self.assertEqual(rep["stats"], {"n_trades": 0})

    def test_rrg_call_caveat(self):
        with _Harness():
            rep = backtest.run_backtest(
                [{"field": "rrg_call", "op": "==", "value": "ROTATE IN"}],
                universe="sp500")
        self.assertTrue(any("rrg_call" in c for c in rep["caveats"]))


if __name__ == "__main__":
    unittest.main()
