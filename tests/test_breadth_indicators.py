"""
Unit tests for breadth indicator math, against small hand-computed examples.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest

import numpy as np
import pandas as pd

from modules.breadth import indicators as ind
from modules.breadth import regime


def _dates(n, start="2026-01-05"):
    return pd.bdate_range(start, periods=n).strftime("%Y-%m-%d")


class TestDailyAggregates(unittest.TestCase):
    def setUp(self):
        # 3 symbols, 4 days. Day-over-day moves are hand-picked:
        #  d1→d2: A up, B down, C up      → adv 2, dec 1
        #  d2→d3: A up, B up,  C unchanged → adv 2, dec 0, unch 1
        #  d3→d4: A down, B down, C down   → adv 0, dec 3
        idx = _dates(4)
        self.close = pd.DataFrame({
            "A": [10.0, 11.0, 12.0, 11.5],
            "B": [20.0, 19.0, 19.5, 19.0],
            "C": [5.0, 6.0, 6.0, 5.5],
        }, index=idx)
        self.volume = pd.DataFrame({
            "A": [100.0, 110.0, 120.0, 130.0],
            "B": [200.0, 210.0, 220.0, 230.0],
            "C": [50.0, 55.0, 60.0, 65.0],
        }, index=idx)

    def test_advance_decline_counts(self):
        agg = ind.daily_aggregates(self.close, self.volume)
        # first day has no prev close → dropped (n_symbols == 0)
        self.assertEqual(len(agg), 3)
        self.assertEqual(list(agg["advances"]), [2, 2, 0])
        self.assertEqual(list(agg["declines"]), [1, 0, 3])
        self.assertEqual(list(agg["unchanged"]), [0, 1, 0])

    def test_up_down_volume(self):
        agg = ind.daily_aggregates(self.close, self.volume)
        # d2: up vol = A(110) + C(55) = 165, down vol = B(210)
        self.assertAlmostEqual(agg["up_vol"].iloc[0], 165.0)
        self.assertAlmostEqual(agg["down_vol"].iloc[0], 210.0)
        # d4: all down → up_vol 0, down vol = 130+230+65 = 425
        self.assertAlmostEqual(agg["up_vol"].iloc[2], 0.0)
        self.assertAlmostEqual(agg["down_vol"].iloc[2], 425.0)

    def test_pct_above_ma_eligibility(self):
        # 25 days of rising prices: with a 20-day window the SMA exists only
        # from day 20 on, and a rising series is always above its SMA.
        idx   = _dates(25)
        close = pd.DataFrame({"A": np.linspace(10, 20, 25),
                              "B": np.linspace(30, 40, 25)}, index=idx)
        vol   = close * 0 + 100
        agg   = ind.daily_aggregates(close, vol)
        self.assertTrue(np.isnan(agg["pct_above_20"].iloc[0]))   # no 20d history yet
        self.assertAlmostEqual(agg["pct_above_20"].iloc[-1], 100.0)

    def test_ema_counts_and_eligibility(self):
        # 25 rising days: a rising series sits above its EMA once the EMA exists.
        idx   = _dates(25)
        close = pd.DataFrame({"A": np.linspace(10, 20, 25),
                              "B": np.linspace(30, 40, 25)}, index=idx)
        vol   = close * 0 + 100
        agg   = ind.daily_aggregates(close, vol)
        self.assertEqual(agg["n_above_5ema"].iloc[-1], 2)          # both names above
        self.assertAlmostEqual(agg["pct_above_5ema"].iloc[-1], 100.0)
        # 20-EMA needs 20 bars of history → first row has no eligible names
        self.assertTrue(np.isnan(agg["pct_above_20ema"].iloc[0]))

    def test_new_highs_need_full_window(self):
        idx   = _dates(ind.NH_NL_WINDOW + 5)
        close = pd.DataFrame({"A": np.linspace(10, 20, len(idx))}, index=idx)
        vol   = close * 0 + 100
        agg   = ind.daily_aggregates(close, vol)
        # rising series: every bar once the window is full is a 252d high
        self.assertEqual(agg["new_highs"].iloc[-1], 1)
        self.assertEqual(agg["new_highs"].iloc[0], 0)   # window not full yet


class TestDerivedChains(unittest.TestCase):
    def _agg(self, adv, dec, upv=None, dnv=None, nh=None, nl=None):
        n = len(adv)
        return pd.DataFrame({
            "advances": adv, "declines": dec,
            "up_vol":   upv or [100.0] * n, "down_vol": dnv or [100.0] * n,
            "new_highs": nh or [0] * n,     "new_lows": nl or [0] * n,
            "n_symbols": [sum(x) for x in zip(adv, dec)],
        }, index=_dates(n))

    def test_mcclellan_hand_computed(self):
        # Constant adv=150, dec=50 → rana = 1000·100/200 = 500 every day.
        # Both EMAs converge on 500; with adjust=False they START at 500, so
        # the oscillator is exactly 0 throughout. Summation steady state is
        # 10× the persistent rana level: 19·500 − 9·500 = 5000.
        agg = self._agg([150] * 10, [50] * 10)
        der = ind.derive(agg)
        self.assertTrue(np.allclose(der["mcclellan"], 0.0))
        self.assertTrue(np.allclose(der["summation"], 5000.0))

    def test_mcclellan_two_step(self):
        # Day 1 rana=500, day 2 rana=-500 (adv/dec flipped).
        # EMA19: 500 + (2/20)·(-500-500) = 400
        # EMA39: 500 + (2/40)·(-500-500) = 450 → osc = -50
        agg = self._agg([150, 50], [50, 150])
        der = ind.derive(agg)
        self.assertAlmostEqual(der["mcclellan"].iloc[0], 0.0)
        self.assertAlmostEqual(der["mcclellan"].iloc[1], -50.0)
        # summation increment must equal the oscillator (cumulative property):
        # day1 = 19·500 − 9·500 = 5000, day2 = 19·450 − 9·400 = 4950 → Δ = −50
        self.assertAlmostEqual(der["summation"].iloc[0], 5000.0)
        self.assertAlmostEqual(
            der["summation"].iloc[1] - der["summation"].iloc[0], -50.0)

    def test_ad_line_cumsum(self):
        agg = self._agg([10, 20, 5], [5, 10, 20])
        der = ind.derive(agg)
        self.assertEqual(list(der["ad_line"]), [5, 15, 0])

    def test_trin(self):
        # adv/dec = 2, upVol/dnVol = 4 → TRIN = 0.5 (heavy up volume)
        agg = self._agg([100], [50], upv=[400.0], dnv=[100.0])
        der = ind.derive(agg)
        self.assertAlmostEqual(der["trin"].iloc[0], 0.5)
        self.assertAlmostEqual(der["ud_vol_ratio"].iloc[0], 4.0)
        self.assertAlmostEqual(der["net_up_vol"].iloc[0], 300.0)

    def test_ad_volume_line(self):
        agg = self._agg([1, 1], [1, 1], upv=[300.0, 100.0], dnv=[100.0, 300.0])
        der = ind.derive(agg)
        self.assertEqual(list(der["ad_vol_line"]), [200.0, 0.0])

    def test_high_low_index(self):
        agg = self._agg([1] * 3, [1] * 3, nh=[9, 1, 5], nl=[1, 9, 5])
        der = ind.derive(agg)
        # NH/(NH+NL)·100 = 90, 10, 50 → 10d SMA with min_periods=1: 90, 50, 50
        self.assertAlmostEqual(der["hl_index"].iloc[0], 90.0)
        self.assertAlmostEqual(der["hl_index"].iloc[1], 50.0)
        self.assertAlmostEqual(der["hl_index"].iloc[2], 50.0)
        self.assertEqual(list(der["nh_nl"]), [8.0, -8.0, 0.0])


class TestZweigBreadthThrust(unittest.TestCase):
    def test_thrust_detected(self):
        idx = _dates(8)
        zbt = pd.Series([0.45, 0.38, 0.45, 0.55, 0.60, 0.63, 0.64, 0.62], index=idx)
        events = ind.zbt_events(zbt)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], idx[5])   # first close above 0.615

    def test_no_thrust_without_oversold_start(self):
        # never dips to 0.40 → crossing 0.615 is not a thrust
        idx = _dates(6)
        zbt = pd.Series([0.50, 0.52, 0.55, 0.58, 0.60, 0.63], index=idx)
        self.assertEqual(ind.zbt_events(zbt), [])

    def test_too_slow_is_no_thrust(self):
        # below 0.40 but the crossing happens > 10 bars later
        idx  = _dates(16)
        vals = [0.38] + [0.5] * 14 + [0.63]
        self.assertEqual(ind.zbt_events(pd.Series(vals, index=idx)), [])


class TestDivergences(unittest.TestCase):
    def test_bearish_divergence_flagged(self):
        # Index grinds to new highs while the A-D line makes a lower high.
        n     = 80
        idx   = _dates(n)
        index = pd.Series(np.concatenate([np.linspace(100, 110, 40),
                                          np.linspace(110, 115, 40)]), index=idx)
        ad    = pd.Series(np.concatenate([np.linspace(0, 50, 40),
                                          np.linspace(50, 20, 40)]), index=idx)
        events = regime.divergences(index, {"A-D line": ad}, lookback=20, min_gap=5)
        bearish = [e for e in events if e["kind"] == "bearish"]
        self.assertTrue(len(bearish) >= 1)
        self.assertTrue(all(e["measure"] == "A-D line" for e in bearish))

    def test_confirming_breadth_no_flag(self):
        # Breadth confirms every index high → no bearish events.
        n     = 80
        idx   = _dates(n)
        index = pd.Series(np.linspace(100, 120, n), index=idx)
        ad    = pd.Series(np.linspace(0, 100, n), index=idx)
        events = regime.divergences(index, {"A-D line": ad}, lookback=20, min_gap=5)
        self.assertEqual([e for e in events if e["kind"] == "bearish"], [])


class TestRegimeState(unittest.TestCase):
    def _series(self, summation_vals, pct200_vals):
        idx = _dates(len(summation_vals))
        return (pd.Series(summation_vals, index=idx, dtype=float),
                pd.Series(pct200_vals, index=idx, dtype=float))

    def test_healthy(self):
        s, p = self._series(np.linspace(100, 500, 30), [70.0] * 30)
        r = regime.regime_state(s, p, active_divergences=0)
        self.assertEqual(r["state"], "HEALTHY")

    def test_deteriorating(self):
        s, p = self._series(np.linspace(200, -300, 30), [45.0] * 30)
        r = regime.regime_state(s, p, active_divergences=2)
        self.assertEqual(r["state"], "DETERIORATING")

    def test_neutral(self):
        s, p = self._series(np.linspace(-50, 60, 30), [55.0] * 30)
        r = regime.regime_state(s, p, active_divergences=0)
        self.assertEqual(r["state"], "NEUTRAL")


class TestRegimeSeries(unittest.TestCase):
    def _series(self, summation_vals, pct200_vals):
        idx = _dates(len(summation_vals))
        return (pd.Series(summation_vals, index=idx, dtype=float),
                pd.Series(pct200_vals, index=idx, dtype=float))

    def test_labels_healthy_and_deteriorating(self):
        # rising positive summation + broad %>200d → HEALTHY at the end;
        # falling negative summation + narrow %>200d → DETERIORATING.
        s, p = self._series(np.linspace(100, 500, 40), [70.0] * 40)
        labels = regime.regime_series(s, p)
        self.assertEqual(labels.iloc[-1], "HEALTHY")
        self.assertEqual(len(labels), 40)

        s2, p2 = self._series(np.linspace(200, -400, 40), [40.0] * 40)
        self.assertEqual(regime.regime_series(s2, p2).iloc[-1], "DETERIORATING")

    def test_agrees_with_regime_state_when_no_divergence(self):
        # Without an active divergence, the vectorized series' last label must
        # match the point-in-time regime_state.
        s, p = self._series(np.linspace(-50, 60, 40), [55.0] * 40)
        live = regime.regime_state(s, p, active_divergences=0)["state"]
        self.assertEqual(regime.regime_series(s, p).iloc[-1], live)


if __name__ == "__main__":
    unittest.main()
