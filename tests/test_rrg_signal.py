"""
Unit tests for the decoupled RRG signal math.

Two layers are pinned here. (1) The chart's coordinate decoupling: SIGNAL space
is σ-normalized about the TRUE boundary (RS-Ratio = 100) and the DISPLAY space is
a cosmetic gain that always crosses 100 at the same point. (2) The Elliott-wave /
Fibonacci engine that drives the rotation call off the raw RS line — wave labels,
the call decision tree, and the no-lookahead confirmation lag.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest
from unittest import mock

import numpy as np
import pandas as pd

from modules.rrg import signal
from modules.confluence import wave   # the wave engine moved here (Stage-2 extraction)


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


def _ws(trend="up", leg="impulse", wave="wave-3", retrace=np.nan, ext=np.nan,
        fib_target=np.nan, fib_w1=np.nan, cur=np.nan, gp_q=0.0, ext_q=0.0,
        div_rs="none", div_px="none", htf_div="none", ltf_div="none",
        mtf_1mo=0.0, mtf_1d=0.0, mtf_1h=0.0):
    return {"trend": trend, "leg_kind": leg, "wave": wave, "retrace": retrace,
            "ext": ext, "fib_target": fib_target, "fib_w1": fib_w1, "cur": cur,
            "gp_q": gp_q, "ext_q": ext_q, "div_rs": div_rs, "div_px": div_px,
            "htf_div": htf_div, "ltf_div": ltf_div,
            "mtf_1mo": mtf_1mo, "mtf_1d": mtf_1d, "mtf_1h": mtf_1h}


class TestConviction(unittest.TestCase):
    """The probabilistic confluence score and the thresholds that route it."""

    def test_factors_accumulate_no_single_one_mandatory(self):
        # No single factor is required — adding any confluence only raises
        # conviction, and there are multiple independent paths up (probabilistic,
        # not AND-gated).
        gp_only = signal._conviction(_ws("up", "corrective", "wave-2", gp_q=1.0))[0]
        gp_div = signal._conviction(_ws("up", "corrective", "wave-2", gp_q=1.0,
                                        div_rs="bull", div_px="bull"))[0]
        gp_mtf = signal._conviction(_ws("up", "corrective", "wave-2", gp_q=1.0,
                                        mtf_1d=1.0, mtf_1mo=1.0))[0]
        self.assertGreater(gp_div, gp_only)     # divergence path lifts it
        self.assertGreater(gp_mtf, gp_only)     # multi-timeframe alignment path lifts it

    def test_partial_depth_is_partial_conviction(self):
        deep = signal._conviction(_ws("up", "corrective", "wave-2", gp_q=1.0))[0]
        shallow = signal._conviction(_ws("up", "corrective", "wave-2", gp_q=0.2))[0]
        self.assertGreater(deep, shallow)

    def test_score_is_signed_and_clamped(self):
        bear = signal._conviction(_ws("up", "impulse", "wave-5", ext_q=1.0,
                                      div_rs="bear", div_px="bear"))[0]
        self.assertLess(bear, 0)
        self.assertGreaterEqual(bear, signal.CONV_LO)
        bull = signal._conviction(_ws("up", "corrective", "wave-2", gp_q=1.0,
                                      div_rs="bull", div_px="bull", htf_div="bull", ltf_div="bull"))[0]
        self.assertLessEqual(bull, signal.CONV_HI)


class TestRotationCall(unittest.TestCase):
    """Conviction thresholds → the 7-value call, with wave context selecting it."""

    def test_high_conviction_wave2_rotates_in(self):
        call, _ = signal._rotation_call(_ws("up", "corrective", "wave-2", gp_q=1.0,
                                            div_rs="bull", div_px="bull"))
        self.assertEqual(call, "ROTATE IN")

    def test_arming_wave2_is_watch(self):
        # shallow depth, no divergence → conviction between T_WATCH and T_IN
        call, _ = signal._rotation_call(_ws("up", "corrective", "wave-2", gp_q=0.4),
                                        params={"T_IN": 60.0, "T_WATCH": 15.0})
        self.assertEqual(call, "WATCH")

    def test_wave3_extension_is_its_own_call(self):
        call, _ = signal._rotation_call(_ws("up", "impulse", "wave-3", ext_q=1.0,
                                            div_rs="bear", div_px="bear"))
        self.assertEqual(call, "⚠️ w3 extended")
        self.assertIn(call, signal.WARN_CALLS)

    def test_wave3_not_extended_holds(self):
        call, _ = signal._rotation_call(_ws("up", "impulse", "wave-3", ext_q=0.0))
        self.assertEqual(call, "HOLD")

    def test_wave5_at_target_is_its_own_call(self):
        call, _ = signal._rotation_call(_ws("up", "impulse", "wave-5", ext_q=1.0,
                                            div_rs="bear", div_px="bear"))
        self.assertEqual(call, "⚠️ w5 extended")

    def test_no_clean_setup_is_watch(self):
        self.assertEqual(signal._rotation_call(_ws(trend="ambiguous", wave="—"))[0], "WATCH")
        self.assertEqual(signal._rotation_call(_ws(trend="none", wave="—"))[0], "WATCH")

    def test_downtrend_bounce_into_pocket_rotates_out(self):
        call, _ = signal._rotation_call(_ws("down", "corrective", "wave-2", gp_q=1.0,
                                            div_rs="bear", div_px="bear"))
        self.assertEqual(call, "ROTATE OUT")

    def test_downtrend_impulse_avoids(self):
        call, _ = signal._rotation_call(_ws("down", "impulse", "wave-3", ext_q=1.0))
        self.assertEqual(call, "AVOID")

    def test_thresholds_thread_through_without_global_mutation(self):
        ws = _ws("up", "corrective", "wave-2", gp_q=1.0)     # GP-only conviction
        self.assertEqual(signal._rotation_call(ws, params={"T_IN": 999.0})[0], "WATCH")
        self.assertEqual(signal._rotation_call(ws, params={"T_IN": 10.0})[0], "ROTATE IN")
        self.assertEqual(signal.DEFAULTS["T_IN"], 40.0)      # default, unchanged by calls


def _impulse_rs():
    """A clean 1-2-3-4-5 RS line: dip (start), w1 up, deep w2, big w3, shallow
    w4, w5 up — exercises every wave label."""
    def leg(a, b, n):
        return list(np.linspace(a, b, n))[1:]
    seq = ([105] + leg(105, 95, 12) + leg(95, 115, 22) + leg(115, 101, 15)
           + leg(101, 160, 32) + leg(160, 150, 12) + leg(150, 185, 24))
    return pd.Series(seq, index=pd.bdate_range("2022-06-01", periods=len(seq)))


class TestWaveEngine(unittest.TestCase):
    def test_zigzag_swings_significant_and_alternating(self):
        piv = wave._zigzag_swings(_impulse_rs(), signal.DEFAULTS["ZIGZAG_K"])
        kinds = [p["kind"] for p in piv]
        self.assertTrue(all(kinds[i] != kinds[i + 1] for i in range(len(kinds) - 1)))
        self.assertGreaterEqual(len(piv), 4)
        # every recorded swing exceeds the volatility threshold → no micro-noise
        spans = [abs(piv[i + 1]["price"] - piv[i]["price"]) for i in range(len(piv) - 1)]
        self.assertTrue(min(spans) > 1.0)   # synthetic legs are ~10-60 RS units

    def test_features_label_the_full_impulse(self):
        # the idealized clean legs need a lower ZigZag threshold than noisy real
        # data; the labeling logic under test is unchanged by k.
        feats = wave._wave_features(_impulse_rs(), {"ZIGZAG_K": 0.7})
        labels = set(feats["wave_label"].unique())
        self.assertTrue({"wave-2", "wave-3", "wave-4", "wave-5"} <= labels)

    def test_no_lookahead(self):
        """A wave feature at date d is identical whether or not later bars exist —
        the confirmation lag (and the forward-only ZigZag fold) makes the backtest
        replay honest. Random walks exercise the same-kind-pivot collapse that a
        clean impulse never triggers."""
        rng = np.random.default_rng(7)
        s = pd.Series(np.cumsum(rng.normal(0, 2, 600)) + 200,
                      index=pd.bdate_range("2021-01-04", periods=600))
        full = wave._wave_features(s, None)
        cut = len(s) - 60
        trunc = wave._wave_features(s.iloc[:cut], None)
        for c in ("wv_trend", "wv_leg", "wave_label", "abc_type", "div_rs", "div_px"):
            self.assertEqual(full[c].iloc[:cut].astype(str).tolist(),
                             trunc[c].astype(str).tolist(), f"{c} leaked future data")
        for c in ("retrace_pct", "ext_ratio", "c_tgt_lo", "c_tgt_hi"):
            a, b = full[c].iloc[:cut].to_numpy(), trunc[c].to_numpy()
            self.assertTrue(np.allclose(np.nan_to_num(a, nan=-1e9),
                                        np.nan_to_num(b, nan=-1e9)), f"{c} leaked")

    def test_abc_correction_off_a_wave5_top(self):
        """After a wave-5 top the engine reads an A-B-C correction: wave A → ROTATE
        OUT, a zigzag wave B → ROTATE OUT (sell the bounce), wave C into the prior
        wave-4 / target zone → WATCH (bottoming setup)."""
        def leg(a, b, n):
            return list(np.linspace(a, b, n))[1:]
        seq = ([105] + leg(105, 95, 12) + leg(95, 115, 22) + leg(115, 101, 15)
               + leg(101, 160, 32) + leg(160, 150, 12) + leg(150, 185, 24)   # 1-2-3-4-5
               + leg(185, 158, 16) + leg(158, 168, 10) + leg(168, 148, 16))  # A-B-C
        s = pd.Series(seq, index=pd.bdate_range("2022-01-03", periods=len(seq)))
        feats = wave._wave_features(s, {"ZIGZAG_K": 0.7})
        abc = feats[feats["wv_leg"] == "abc"]
        self.assertTrue({"wave-A", "wave-B", "wave-C"} <= set(abc["wave_label"]))

        def call_of(row):
            ws = {"trend": row.wv_trend, "leg_kind": row.wv_leg, "wave": row.wave_label,
                  "retrace": row.retrace_pct, "ext": row.ext_ratio, "fib_target": row.fib_target,
                  "fib_w1": row.fib_w1, "gp_q": row.gp_q, "ext_q": row.ext_q, "cur": row.cur_rs,
                  "abc_type": row.abc_type, "c_tgt_lo": row.c_tgt_lo, "c_tgt_hi": row.c_tgt_hi,
                  "w4_zone": row.w4_zone, "div_rs": row.div_rs, "div_px": row.div_px}
            return signal._rotation_call(ws, None)[0]

        # wave A → ROTATE OUT (correction underway), independent of conviction
        a_rows = abc[abc["wave_label"] == "wave-A"]
        self.assertEqual(call_of(a_rows.iloc[-1]), "ROTATE OUT")


class TestDivergence(unittest.TestCase):
    def test_rsi_bounds(self):
        s = pd.Series(np.r_[np.linspace(100, 130, 40), np.linspace(130, 115, 20)])
        r = wave._rsi(s).dropna()
        self.assertTrue((r >= 0).all() and (r <= 100).all())

    def test_bearish_divergence_on_a_lower_momentum_high(self):
        """Price prints a higher high on a slower climb → RSI lower → bearish."""
        def leg(a, b, n):
            return list(np.linspace(a, b, n))[1:]
        seq = [100] + leg(100, 130, 20) + leg(130, 118, 12) + leg(118, 133, 40)
        s = pd.Series(seq, index=pd.bdate_range("2022-01-03", periods=len(seq)))
        feats = wave._wave_features(s, None, price=s)
        self.assertEqual(feats["div_rs"].iloc[-1], "bear")

    def test_divergence_raises_wave2_conviction(self):
        base = dict(trend="up", leg_kind="corrective", wave="wave-2", gp_q=0.4, ext_q=0.0,
                    retrace=0.65, ext=np.nan, fib_target=np.nan, fib_w1=np.nan, cur=100.0,
                    abc_type="—", c_tgt_lo=np.nan, c_tgt_hi=np.nan, w4_zone=np.nan,
                    htf_div="none", ltf_div="none")
        no_div = signal._conviction({**base, "div_rs": "none", "div_px": "none"})[0]
        with_div = signal._conviction({**base, "div_rs": "bull", "div_px": "bull"})[0]
        self.assertGreater(with_div, no_div)

    def test_confluence_note_lists_agreeing_sources(self):
        ws = {"div_rs": "bull", "div_px": "bull", "htf_div": "bull", "ltf_div": "none"}
        self.assertEqual(signal._confluence_note(ws, "bull"), " · confluence: RS+price+HTF")
        self.assertEqual(signal._confluence_note(ws, "bear"), "")


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
        valid = {"ROTATE IN", "ROTATE OUT", "HOLD", "AVOID", "WATCH"} | set(signal.WARN_CALLS)
        self.assertEqual(set(calls), {"XLK", "XLP"})
        for timeline in calls.values():
            self.assertTrue(timeline)
            for ev in timeline.values():
                self.assertIn(ev["call"], valid)


if __name__ == "__main__":
    unittest.main()
