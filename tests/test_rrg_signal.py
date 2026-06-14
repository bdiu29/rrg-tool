"""
Unit tests for the decoupled RRG signal math.

The point of the refactor: the rotation calls run on a SIGNAL space that is
σ-normalized about the TRUE boundary (RS-Ratio = 100), interval-independent, and
honest at the quadrant line — while the chart's DISPLAY space is a cosmetic gain
that always crosses 100 at the same point. These tests pin those invariants.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest
from unittest import mock

import numpy as np
import pandas as pd

from modules.rrg import signal


# ---------------------------------------------------------------------------
# Pure helpers — no data needed
# ---------------------------------------------------------------------------

class TestScaleInvariance(unittest.TestCase):
    def test_sigma_normalization_is_amplitude_invariant(self):
        """A move scaled by any positive constant produces the same σ-coords —
        that's what lets one gate set mean the same on daily and weekly."""
        rng = np.random.default_rng(0)
        base = pd.Series(100 + np.cumsum(rng.normal(0, 0.3, 400)))
        a = signal._scale(base, 120, 100.0)
        b = signal._scale((base - 100) * 7.0 + 100, 120, 100.0)   # 7× amplitude
        common = a.dropna().index.intersection(b.dropna().index)
        self.assertTrue(len(common) > 100)
        np.testing.assert_allclose(a.loc[common], b.loc[common], rtol=1e-6, atol=1e-6)

    def test_scale_centers_on_the_true_boundary(self):
        s = pd.Series([100.0] * 200)        # exactly at the boundary
        out = signal._scale(s + 0.0, 100, 100.0)
        # all deviations are zero → σ-coord is 0 (or NaN where σ==0), never >0
        self.assertTrue(((out.fillna(0.0)) == 0.0).all())


class TestQuadrantBoundary(unittest.TestCase):
    def test_boundary_is_exactly_100(self):
        self.assertEqual(signal._quadrant(100.0, 100.0), "Leading")
        self.assertEqual(signal._quadrant(99.9, 100.0), "Improving")  # ratio<100
        self.assertEqual(signal._quadrant(100.0, 99.9), "Weakening")  # mom<100
        self.assertEqual(signal._quadrant(99.9, 99.9), "Lagging")


class TestRotationCall(unittest.TestCase):
    def test_committed_leg_rotates_in(self):
        ratios  = [97.0, 97.5, 98.0, 98.5, 99.0, 99.5]      # rising, still <100 (Improving)
        moments = [100.2, 100.6, 101.0, 101.4, 101.8, 102.2]
        q = signal._quadrant(ratios[-1], moments[-1])
        self.assertEqual(q, "Improving")
        _, heading, _ = signal._tail_heading(ratios, moments)
        call, _ = signal._rotation_call(
            ratios, moments, q,
            signal._accum(ratios, moments), signal._distrib(ratios, moments),
            heading, phase="impulse ↑ (wave 3/5)")
        self.assertEqual(call, "ROTATE IN")

    def test_corrective_chop_does_not_rotate(self):
        ratios  = [97.0, 98.0, 97.4, 98.2, 97.6, 98.1]      # zig-zag → low directness
        moments = [100.1, 101.0, 100.4, 101.1, 100.5, 101.0]
        q = signal._quadrant(ratios[-1], moments[-1])
        self.assertEqual(q, "Improving")
        _, heading, _ = signal._tail_heading(ratios, moments)
        call, _ = signal._rotation_call(
            ratios, moments, q,
            signal._accum(ratios, moments), signal._distrib(ratios, moments),
            heading, phase="bounce (wave B)")
        self.assertNotEqual(call, "ROTATE IN")

    def test_params_thread_through_without_global_mutation(self):
        """A tiny net move clears a low MOVE_GATE but not a high one — and the
        module DEFAULTS are untouched afterward (thread-safe for live requests)."""
        ratios  = [99.0, 99.1, 99.2, 99.3, 99.4, 99.5]      # all <100 → Improving
        moments = [100.0, 100.1, 100.2, 100.3, 100.4, 100.5]
        q = signal._quadrant(ratios[-1], moments[-1])
        self.assertEqual(q, "Improving")
        _, heading, _ = signal._tail_heading(ratios, moments)
        args = (ratios, moments, q, signal._accum(ratios, moments),
                signal._distrib(ratios, moments), heading, "impulse ↑ (wave 3/5)")
        loose, _ = signal._rotation_call(*args, params={"MOVE_GATE": 0.1, "DIRECT_GATE": 0.3})
        tight, _ = signal._rotation_call(*args, params={"MOVE_GATE": 5.0})
        self.assertEqual(loose, "ROTATE IN")
        self.assertNotEqual(tight, "ROTATE IN")
        self.assertEqual(signal.DEFAULTS["MOVE_GATE"], 0.90)   # unchanged


# ---------------------------------------------------------------------------
# compute_series / replay_calls — with a synthetic price panel
# ---------------------------------------------------------------------------

def _synthetic_close(n=440, seed=1):
    """SPY drifts up; XLK out-drifts it (a steady leader); XLP under-drifts (a
    steady laggard). Small noise keeps σ finite."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=n)
    def path(drift):
        r = drift + rng.normal(0, 0.006, n)
        return 100.0 * np.exp(np.cumsum(r))
    return pd.DataFrame({
        "SPY": path(0.0003),
        "XLK": path(0.0009),     # leader
        "XLP": path(-0.0004),    # laggard
    }, index=idx)


class TestComputeSeries(unittest.TestCase):
    def setUp(self):
        self.close = _synthetic_close()
        self._patch = mock.patch.object(signal, "_fetch_close",
                                        return_value=self.close)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_display_boundary_is_honest(self):
        """For every point, the display coord sits on the same side of 100 as
        the signal coord — so the drawn cross and the calls never disagree."""
        series, _, _ = signal.compute_series(["XLK", "XLP"], "SPY", "1d")
        for df in series.values():
            self.assertTrue(len(df) > 10)
            np.testing.assert_array_equal(
                np.sign((df["disp_ratio"] - 100).round(6)),
                np.sign((df["sig_ratio"] - 100).round(6)))
            np.testing.assert_array_equal(
                np.sign((df["disp_mom"] - 100).round(6)),
                np.sign((df["sig_mom"] - 100).round(6)))

    def test_steady_leader_and_laggard_land_on_the_right_side(self):
        """The honest boundary: a persistent out-performer reads above 100, a
        persistent laggard below — what the old rolling-mean weekly z-score got
        wrong (it measured 'above its own average', not 'fast EMA > slow')."""
        series, _, _ = signal.compute_series(["XLK", "XLP"], "SPY", "1d")
        self.assertGreater(series["XLK"]["sig_ratio"].tail(6).mean(), 100.0)
        self.assertLess(series["XLP"]["sig_ratio"].tail(6).mean(), 100.0)

    def test_replay_calls_emits_valid_calls(self):
        series, _, _ = signal.compute_series(["XLK", "XLP"], "SPY", "1d")
        calls = signal.replay_calls(series, tail=6)
        valid = {"ROTATE IN", "ROTATE OUT", "HOLD", "AVOID", "WATCH"}
        self.assertEqual(set(calls), {"XLK", "XLP"})
        for timeline in calls.values():
            self.assertTrue(timeline)
            for ev in timeline.values():
                self.assertIn(ev["call"], valid)


if __name__ == "__main__":
    unittest.main()
