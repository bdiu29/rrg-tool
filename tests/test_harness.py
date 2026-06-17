"""Unit tests for the AI subagent harness — the deterministic combiner (the
backtestable core), the fail-soft vote adapters, and the LLM layer's template
fallbacks. No network, no real `claude` CLI call (the subprocess is mocked)."""

import unittest
from unittest import mock

from modules.harness import combiner, votes, agents


# ---------------------------------------------------------------------------
# Combiner — the deterministic decision (math decides)
# ---------------------------------------------------------------------------

def _v(domain, direction, conviction, weight, ok=True, detail=None):
    return {"domain": domain, "direction": direction, "conviction": conviction,
            "weight": weight, "ok": ok, "detail": detail or {}}


class TestStanceArbiter(unittest.TestCase):
    def test_rotate_only_when_broad_and_rotation_on(self):
        self.assertEqual(combiner.decide_stance("HEALTHY", "on"), "ROTATE")

    def test_concentrate_when_narrow_or_rotation_off(self):
        self.assertEqual(combiner.decide_stance("DETERIORATING", "on"), "CONCENTRATE")
        self.assertEqual(combiner.decide_stance("HEALTHY", "off"), "CONCENTRATE")

    def test_neutral_and_failsoft(self):
        self.assertEqual(combiner.decide_stance("NEUTRAL", None), "NEUTRAL")
        self.assertEqual(combiner.decide_stance(None, None), "NEUTRAL")

    def test_stance_factor_suppresses_rotation_bet_when_concentrated(self):
        self.assertEqual(combiner._stance_factor("rrg", "CONCENTRATE"), 0.5)
        self.assertEqual(combiner._stance_factor("rrg", "ROTATE"), 1.0)
        self.assertGreater(combiner._stance_factor("canslim", "CONCENTRATE"), 1.0)


class TestCombine(unittest.TestCase):
    def setUp(self):
        self.votes = [
            _v("breadth", 1, 80, 30),       # +0.8 * 30 = 24
            _v("rrg", 1, 50, 20),           # +0.5 * 20 * stance_factor
            _v("news", -1, 50, 12),         # -0.5 * 12 = -6
            _v("flow", 0, 90, 10),          # 0 → ignored
            _v("screener", 1, 40, 8, ok=False),  # no data → skipped
        ]

    def test_weighted_sum_in_rotate_regime(self):
        out = combiner.combine(self.votes, "HEALTHY", "on")
        self.assertEqual(out["stance"], "ROTATE")
        self.assertAlmostEqual(out["score"], 28.0)      # 24 + 10 - 6
        self.assertEqual(out["n_votes"], 4)             # the ok=False vote is excluded

    def test_concentrate_halves_the_rrg_rotation_bet(self):
        out = combiner.combine(self.votes, "DETERIORATING", "off")
        self.assertEqual(out["stance"], "CONCENTRATE")
        self.assertAlmostEqual(out["score"], 23.0)      # 24 + 5 - 6 (rrg halved)

    def test_failsoft_vote_excluded_from_factors(self):
        out = combiner.combine(self.votes, "HEALTHY", "on")
        self.assertNotIn("screener", [f[0] for f in out["factors"]])
        self.assertNotIn("flow", [f[0] for f in out["factors"]])   # zero contribution dropped

    def test_score_is_clamped(self):
        big = [_v("breadth", 1, 100, 400)]
        self.assertEqual(combiner.combine(big, "HEALTHY", "on")["score"], combiner.SCORE_HI)
        neg = [_v("breadth", -1, 100, 400)]
        self.assertEqual(combiner.combine(neg, "HEALTHY", "on")["score"], combiner.SCORE_LO)

    def test_posture_bands(self):
        self.assertEqual(combiner._posture(50), "Risk-on")
        self.assertEqual(combiner._posture(0), "Neutral / mixed")
        self.assertEqual(combiner._posture(-60), "Risk-off")


class TestSectorConfluence(unittest.TestCase):
    def test_agreement_bonus_and_longs_avoids(self):
        vs = [
            _v("rrg", 1, 0, 20, detail={"sectors": [
                {"ticker": "XLK", "name": "Tech", "call": "ROTATE IN", "conviction": 60},
                {"ticker": "XLU", "name": "Util", "call": "ROTATE OUT", "conviction": 40},
            ]}),
            _v("rankings", 1, 0, 15, detail={"sectors": [
                {"ticker": "XLK", "rank": 90}, {"ticker": "XLU", "rank": 20},
            ]}),
        ]
        longs, avoids = combiner._sector_confluence(vs)
        self.assertEqual(longs[0]["ticker"], "XLK")
        self.assertTrue(longs[0]["agree"])
        self.assertAlmostEqual(longs[0]["score"], 105.0)    # (60 + 40) * 1.25
        self.assertEqual(avoids[0]["ticker"], "XLU")
        self.assertLess(avoids[0]["score"], 0)

    def test_no_rrg_detail_returns_empty(self):
        self.assertEqual(combiner._sector_confluence([_v("breadth", 1, 50, 30)]), ([], []))


# ---------------------------------------------------------------------------
# Votes — fail-soft adapters reusing each module's entrypoint
# ---------------------------------------------------------------------------

class TestVoteHelpers(unittest.TestCase):
    def test_vote_clamps_and_sets_weight(self):
        v = votes._vote("breadth", 1, 150)
        self.assertEqual(v["conviction"], 100.0)            # clamped to 100
        self.assertEqual(v["weight"], votes.WEIGHTS["breadth"])
        self.assertTrue(v["ok"])

    def test_fail_is_not_ok(self):
        v = votes._fail("rrg", "boom")
        self.assertFalse(v["ok"])
        self.assertEqual(v["direction"], 0)
        self.assertIn("boom", v["note"])


class TestVoteBreadth(unittest.TestCase):
    def test_success_maps_regime_to_direction(self):
        fake = {"regime": "HEALTHY", "score": 4, "reasons": ["Summation rising"],
                "interpretation": "buy dips", "metrics": {}, "active_divergences": []}
        with mock.patch("modules.breadth.build_summary", return_value=fake):
            v = votes.vote_breadth()
        self.assertTrue(v["ok"])
        self.assertEqual(v["direction"], 1)
        self.assertEqual(v["regime_context"], "HEALTHY")
        self.assertGreater(v["conviction"], 0)

    def test_deteriorating_is_bearish(self):
        fake = {"regime": "DETERIORATING", "score": 3, "reasons": [], "metrics": {}}
        with mock.patch("modules.breadth.build_summary", return_value=fake):
            self.assertEqual(votes.vote_breadth()["direction"], -1)

    def test_failsoft_on_exception(self):
        with mock.patch("modules.breadth.build_summary", side_effect=RuntimeError("db")):
            v = votes.vote_breadth()
        self.assertFalse(v["ok"])


class TestGatherAll(unittest.TestCase):
    def test_never_raises_and_resolves_regime(self):
        # every adapter raises → all votes ok=False, gather_all still returns cleanly
        bombs = [lambda: (_ for _ in ()).throw(ValueError("x"))]
        with mock.patch.object(votes, "_ADAPTERS", bombs), \
             mock.patch("modules.rrg.signal.current_regime", return_value="NEUTRAL"), \
             mock.patch("modules.rrg.signal.rotation_regime", return_value="off"):
            vlist, regime, rotation = votes.gather_all()
        self.assertEqual(regime, "NEUTRAL")
        self.assertEqual(rotation, "off")
        self.assertFalse(vlist[0]["ok"])


# ---------------------------------------------------------------------------
# Agents — LLM layer; the CLI subprocess is never really invoked
# ---------------------------------------------------------------------------

class TestAgents(unittest.TestCase):
    def test_available_false_without_binary(self):
        with mock.patch.object(agents, "_binary", return_value=None):
            self.assertFalse(agents.available())

    def test_available_false_when_disabled(self):
        with mock.patch.dict("os.environ", {"HARNESS_LLM": "0"}), \
             mock.patch.object(agents, "_binary", return_value="/x/claude"):
            self.assertFalse(agents.available())

    def test_claude_cli_none_without_binary(self):
        with mock.patch.object(agents, "_binary", return_value=None):
            self.assertIsNone(agents.claude_cli("hi", "haiku"))

    def test_claude_cli_parses_json_result(self):
        proc = mock.Mock(returncode=0, stdout='{"result":"the answer","is_error":false}')
        with mock.patch.object(agents, "_binary", return_value="/x/claude"), \
             mock.patch("subprocess.run", return_value=proc):
            self.assertEqual(agents.claude_cli("hi", "haiku"), "the answer")

    def test_claude_cli_none_on_error_flag(self):
        proc = mock.Mock(returncode=0, stdout='{"result":"x","is_error":true}')
        with mock.patch.object(agents, "_binary", return_value="/x/claude"), \
             mock.patch("subprocess.run", return_value=proc):
            self.assertIsNone(agents.claude_cli("hi", "haiku"))

    def test_master_brief_falls_back_to_template_when_cli_returns_none(self):
        combined = {"score": 28.0, "posture": "Lean bullish", "stance": "ROTATE",
                    "regime": "HEALTHY", "rotation": "on", "factors": [["breadth", 24]],
                    "longs": [{"ticker": "XLK", "call": "ROTATE IN", "rank": 90}],
                    "avoids": []}
        with mock.patch.object(agents, "available", return_value=True), \
             mock.patch.object(agents, "claude_cli", return_value=None):
            text, used = agents.master_brief([], combined, "HEALTHY", "on")
        self.assertFalse(used)
        self.assertIn("ROTATE", text)
        self.assertIn("XLK", text)

    def test_template_rationale_for_ok_vote(self):
        v = _v("rrg", 1, 55, 20)
        v["factors"] = [["XLK ROTATE IN", 55]]
        self.assertIn("bullish", agents._template_rationale(v))


if __name__ == "__main__":
    unittest.main()
