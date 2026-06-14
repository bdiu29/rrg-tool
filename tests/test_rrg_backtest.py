"""
Unit tests for the RRG strategy backtester.

A single deterministic ticker with one engineered ROTATE IN → ROTATE OUT cycle
lets us assert exact next-bar-open execution (no lookahead), the forward-return
event study, and the exit models. The signal timeline and OHLC are mocked, so
nothing hits the network.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest
from unittest import mock

import numpy as np
import pandas as pd

from modules.rrg import backtest


N = 30
IDX = pd.bdate_range("2026-01-02", periods=N)


def _ohlc():
    """XLK close ramps 100,101,…; open[i] = close[i-1] (the bar we assert the
    entry price against). SPY ramps more slowly (for the excess calc)."""
    c = pd.DataFrame(
        {"XLK": [100.0 + i for i in range(N)],
         "SPY": [50.0 + 0.25 * i for i in range(N)]},
        index=IDX)
    o = c.shift(1)
    o.iloc[0] = c.iloc[0]
    return {"open": o, "high": c + 1.0, "low": c - 1.0, "close": c}


def _timeline():
    """WATCH through the 14-bar ATR warmup, then ROTATE IN onset at bar 16,
    HOLD, ROTATE OUT onset at bar 25."""
    tl = {}
    for i in range(N):
        if i < 16:    call = "WATCH"
        elif i == 16: call = "ROTATE IN"
        elif i < 25:  call = "HOLD"
        elif i == 25: call = "ROTATE OUT"
        else:         call = "AVOID"
        tl[IDX[i]] = {"call": call, "quadrant": "", "phase": ""}
    return {"XLK": tl}


def _run(exit_model="signal"):
    with mock.patch.object(backtest.signal, "compute_series",
                           return_value=({"XLK": pd.DataFrame({"x": [1]})}, "2026", None)), \
         mock.patch.object(backtest.signal, "fetch_ohlc", return_value=_ohlc()), \
         mock.patch.object(backtest.signal, "replay_calls", return_value=_timeline()):
        return backtest.run_backtest(interval="1d", tail=6,
                                     exit_cfg={"model": exit_model})


class TestExecutionNoLookahead(unittest.TestCase):
    def test_signal_exit_enters_next_open_exits_on_opposing_call(self):
        rep = _run("signal")
        self.assertEqual(rep["n_trades_total"], 1)
        t = rep["trades"][0]
        # IN onset on bar 16 → enter at bar 17 open == close[16] == 116
        self.assertEqual(t["entry_date"], IDX[17].strftime("%Y-%m-%d"))
        self.assertAlmostEqual(t["entry_px"], 116.0, places=6)
        # OUT onset on bar 25 → exit at bar 26 open == close[25] == 125
        self.assertEqual(t["exit_date"], IDX[26].strftime("%Y-%m-%d"))
        self.assertAlmostEqual(t["exit_px"], 125.0, places=6)
        self.assertEqual(t["exit_reason"], "signal")
        self.assertAlmostEqual(t["return_pct"], (125.0 / 116.0 - 1) * 100, places=4)
        self.assertEqual(t["bars_held"], 10)

    def test_hold_model_exits_after_n_bars(self):
        rep = _run("hold")              # default hold_days = 10
        t = rep["trades"][0]
        self.assertEqual(t["bars_held"], 10)
        self.assertEqual(t["exit_reason"], "hold")
        self.assertAlmostEqual(t["exit_px"], 126.0, places=6)   # close[26]

    def test_atr_model_hits_target(self):
        rep = _run("atr")              # entry 116, ATR≈2 → target 116+3·2 = 122
        t = rep["trades"][0]
        self.assertEqual(t["exit_reason"], "target")
        self.assertAlmostEqual(t["exit_px"], 122.0, places=6)


class TestEventStudy(unittest.TestCase):
    def test_forward_returns_measured_from_entry_bar(self):
        rep = _run("signal")
        es = rep["event_study"]["ROTATE IN"]
        self.assertEqual(es["n_onsets"], 1)
        # base = close[17] = 117; +5d = close[22] = 122
        exp5 = (122.0 / 117.0 - 1) * 100
        self.assertAlmostEqual(es["horizons"]["5"]["mean"], exp5, places=3)
        # excess vs SPY: spy[17]=54.25, spy[22]=55.5
        exp5_ex = exp5 - (55.5 / 54.25 - 1) * 100
        self.assertAlmostEqual(es["horizons"]["5"]["excess"], exp5_ex, places=3)
        # +20d would need bar 37 (> N) → not recorded (no lookahead past data)
        self.assertNotIn("20", es["horizons"])


class TestHelpers(unittest.TestCase):
    def test_entry_lag(self):
        self.assertEqual(backtest._entry_lag("1d"), pd.Timedelta(0))
        self.assertEqual(backtest._entry_lag("1wk"), pd.Timedelta(days=4))

    def test_separation_in_minus_out(self):
        def rec(call, ex):
            return {"call": call, "date": pd.Timestamp("2026-03-01"), "excess": {10: ex}}
        recs = [rec("ROTATE IN", 3.0), rec("ROTATE IN", 1.0), rec("ROTATE IN", 2.0),
                rec("ROTATE OUT", -1.0), rec("ROTATE OUT", -2.0), rec("ROTATE OUT", -3.0)]
        sep = backtest._separation(recs, pd.Timestamp.min, pd.Timestamp.max)
        self.assertAlmostEqual(sep, 2.0 - (-2.0), places=6)

    def test_separation_nan_when_too_thin(self):
        def rec(call, ex):
            return {"call": call, "date": pd.Timestamp("2026-03-01"), "excess": {10: ex}}
        recs = [rec("ROTATE IN", 3.0), rec("ROTATE OUT", -1.0)]      # < min_n each
        self.assertTrue(np.isnan(backtest._separation(recs, pd.Timestamp.min, pd.Timestamp.max)))


if __name__ == "__main__":
    unittest.main()
