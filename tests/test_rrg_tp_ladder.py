"""
Unit tests for the take-profit ladder exit comparison.

`_tp_schedule` is tested directly in bar-space (no searchsorted) so the four
trigger mechanics are pinned exactly; `_simulate_scaled` + `_scaled_equity_curve`
get an integration check (entry detection, blended fractional return, and that a
full-size position reproduces a plain buy-hold mark).

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest

import numpy as np
import pandas as pd

from modules.rrg import backtest


N = 40
IDX = pd.bdate_range("2026-01-02", periods=N)


def _arrays(close_list):
    c = np.array(close_list, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])      # open[i] = close[i-1]
    return o, c + 1.0, c - 1.0, c             # o, h, l, c


def _fracs(sched):
    return [round(f, 6) for _, f, _ in sched]


def _bars(sched):
    return [b for b, _, _ in sched]


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.o, self.h, self.l, self.c = _arrays([100.0 + i for i in range(N)])

    def test_full_exits_on_first_exit_call(self):
        ob = [(2, "ROTATE IN"), (8, "ROTATE OUT")]
        s = backtest._tp_schedule("full", 2, 30, self.o[2], self.o, self.h, self.l, self.c, ob)
        self.assertEqual(_fracs(s), [1.0])
        self.assertEqual(_bars(s), [8])               # the ROTATE OUT bar

    def test_full_rides_to_terminal_without_exit_call(self):
        ob = [(2, "ROTATE IN"), (5, "HOLD")]
        s = backtest._tp_schedule("full", 2, 25, self.o[2], self.o, self.h, self.l, self.c, ob)
        self.assertEqual(_bars(s), [25])

    def test_calls_ladder_trims_each_escalation(self):
        ob = [(2, "ROTATE IN"), (4, "⚠️ w3 extended"), (6, "⚠️ w5 extended"),
              (8, "ROTATE OUT"), (10, "AVOID")]
        s = backtest._tp_schedule("calls", 2, 30, self.o[2], self.o, self.h, self.l, self.c, ob)
        self.assertEqual(_fracs(s), [0.2, 0.2, 0.2, 0.2, 0.2])
        self.assertEqual(_bars(s), [4, 6, 8, 10, 30])    # last 20% dumped at terminal
        self.assertAlmostEqual(sum(f for _, f, _ in s), 1.0, places=9)

    def test_calls_skipped_rung_sells_down_to_ceiling(self):
        ob = [(2, "ROTATE IN"), (5, "ROTATE OUT")]       # straight to ROTATE OUT (ceiling 0.4)
        s = backtest._tp_schedule("calls", 2, 30, self.o[2], self.o, self.h, self.l, self.c, ob)
        self.assertEqual(_fracs(s), [0.6, 0.4])           # sell 0.6 to reach 0.4, rest at terminal
        self.assertEqual(_bars(s), [5, 30])

    def test_post_out_sells_20pct_per_bar_for_five_bars(self):
        ob = [(2, "ROTATE IN"), (5, "ROTATE OUT")]
        s = backtest._tp_schedule("post_out", 2, 30, self.o[2], self.o, self.h, self.l, self.c, ob)
        self.assertEqual(_bars(s), [5, 6, 7, 8, 9])
        self.assertEqual(_fracs(s), [0.2, 0.2, 0.2, 0.2, 0.2])

    def test_post_out_dumps_remainder_when_terminal_cuts_in(self):
        ob = [(2, "ROTATE IN"), (5, "ROTATE OUT")]
        s = backtest._tp_schedule("post_out", 2, 7, self.o[2], self.o, self.h, self.l, self.c, ob)
        self.assertEqual(_bars(s), [5, 6, 7])
        self.assertEqual(_fracs(s), [0.2, 0.2, 0.6])      # terminal at 7 dumps the rest

    def test_post_out_no_exit_call_holds_full_to_terminal(self):
        ob = [(2, "ROTATE IN"), (6, "HOLD")]
        s = backtest._tp_schedule("post_out", 2, 25, self.o[2], self.o, self.h, self.l, self.c, ob)
        self.assertEqual(_fracs(s), [1.0])
        self.assertEqual(_bars(s), [25])


class TestFibSchedule(unittest.TestCase):
    def test_fib_tags_each_extension_target(self):
        # entry 100, trailing-low anchor 90 ⇒ leg 10 ⇒ targets 106.18/110/116.18/120/126.18
        c = [90.0] * 20 + [100.0] + [108, 112, 118, 122, 128] + [130.0] * (N - 26)
        o, h, l, _c = _arrays(c)
        ei = 20
        self.assertAlmostEqual(o[ei], 90.0)               # open[20] = close[19] = 90
        # force entry price to 100 by checking schedule with entry_px=100 explicitly
        ob = [(ei, "ROTATE IN")]
        s = backtest._tp_schedule("fib", ei, 35, 100.0, o, h, l, _c, ob)
        self.assertEqual(len(s), 5)
        self.assertEqual(_fracs(s), [0.2, 0.2, 0.2, 0.2, 0.2])
        leg = 100.0 - float(np.nanmin(l[ei - backtest.FIB_LOOKBACK:ei]))   # entry − trailing low
        fills = [px for _, _, px in s]
        for got, mult in zip(fills, backtest.FIB_MULTS):
            self.assertAlmostEqual(got, 100.0 + leg * mult, places=6)

    def test_fib_no_leg_falls_back_to_exit(self):
        o, h, l, c = _arrays([100.0 - i for i in range(N)])   # falling → anchor ≥ entry, no leg
        ob = [(20, "ROTATE IN"), (24, "ROTATE OUT")]
        s = backtest._tp_schedule("fib", 20, 35, o[20], o, h, l, c, ob)
        self.assertEqual(_fracs(s), [1.0])
        self.assertEqual(_bars(s), [24])


class TestIntegration(unittest.TestCase):
    def _frames_calls(self):
        c = [100.0 * 1.01 ** i for i in range(N)]
        o, h, l, cc = _arrays(c)
        frames = {"XLK": (o, h, l, cc)}
        tl = {}
        for i in range(N):
            call = "ROTATE IN" if i == 5 else ("ROTATE OUT" if i == 12 else
                   ("HOLD" if 5 < i < 12 else "WATCH"))
            tl[IDX[i]] = {"call": call, "conviction": 50.0, "quadrant": "", "phase": ""}
        return frames, {"XLK": tl}

    def test_full_trade_blended_return_and_equity(self):
        frames, calls = self._frames_calls()
        cfg = backtest._exit_cfg({"model": "signal"})
        trades = backtest._simulate_scaled(calls, frames, IDX, cfg, pd.Timedelta(0), "full")
        self.assertEqual(len(trades), 1)
        t = trades[0]
        # IN at idx[5] → entry bar 6 (open=close[5]); ROTATE OUT at idx[12] → exit bar 13
        o, h, l, c = frames["XLK"]
        self.assertEqual(t["entry_bar"], 6)
        self.assertEqual(t["exit_bar"], 13)
        self.assertAlmostEqual(t["return_pct"], (c[13] / o[6] - 1) * 100, places=4)

        eqc = backtest._scaled_equity_curve(trades, pd.DataFrame({"XLK": c}, index=IDX),
                                            pd.Series(c, index=IDX), IDX)
        # full-size single position ⇒ equity = close-to-close compounding over held bars
        held = c[7:14] / c[6:13]                       # marks from entry+1 (bar 7) to exit (13)
        self.assertAlmostEqual(eqc["total_return"], (np.prod(held) - 1) * 100, places=3)

    def test_comparison_returns_all_triggers(self):
        frames, calls = self._frames_calls()
        cfg = backtest._exit_cfg({"model": "signal"})
        close = pd.DataFrame({"XLK": frames["XLK"][3]}, index=IDX)
        rep = backtest._tp_comparison(calls, frames, close, pd.Series(frames["XLK"][3], index=IDX),
                                      IDX, cfg, pd.Timedelta(0))
        keys = {r["key"] for r in rep["rows"]}
        self.assertEqual(keys, {"full", "calls", "post_out", "fib"})
        self.assertIn(rep["best"]["return"], keys)
        self.assertIn("overlay", rep)


if __name__ == "__main__":
    unittest.main()
