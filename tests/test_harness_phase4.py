"""Phase 4 tests — the autonomous paper daemon's due-gate, harness settings, the
grounded chat's fail-soft, and the suggest() cache. No network (the LLM + data fetch
are mocked/injected)."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from modules.harness import paper_poller, chat, picks, store

_ET = ZoneInfo("America/New_York")


def _dt(weekday, hour, minute):
    """A tz-aware ET datetime on the given weekday (0=Mon … 6=Sun)."""
    d = datetime(2026, 6, 15, hour, minute, tzinfo=_ET)      # anchor, then walk forward
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


class TestIsDue(unittest.TestCase):
    def test_autonomous_weekday_after_close_not_stepped(self):
        d = _dt(2, 16, 30)                                   # Wed 16:30 ET
        self.assertTrue(paper_poller.is_due(d, "autonomous", None, d.date().isoformat()))

    def test_manual_never(self):
        d = _dt(2, 16, 30)
        self.assertFalse(paper_poller.is_due(d, "manual", None, d.date().isoformat()))

    def test_weekend_no(self):
        d = _dt(5, 17, 0)                                    # Saturday
        self.assertFalse(paper_poller.is_due(d, "autonomous", None, d.date().isoformat()))

    def test_before_close_no(self):
        d = _dt(2, 12, 0)                                    # Wed noon (before 16:05)
        self.assertFalse(paper_poller.is_due(d, "autonomous", None, d.date().isoformat()))

    def test_already_stepped_today_no(self):
        d = _dt(2, 16, 30)
        today = d.date().isoformat()
        self.assertFalse(paper_poller.is_due(d, "autonomous", today, today))


class TestSettings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (store.DB_PATH, store._DATA_DIR)
        store.DB_PATH = Path(self._tmp.name) / "trading.db"
        store._DATA_DIR = Path(self._tmp.name)
        store.init_db()

    def tearDown(self):
        store.DB_PATH, store._DATA_DIR = self._orig
        self._tmp.cleanup()

    def test_default_mode_is_manual(self):
        self.assertEqual(store.get_setting("trading_mode", "manual"), "manual")

    def test_set_get_roundtrip_and_upsert(self):
        store.set_setting("trading_mode", "autonomous")
        self.assertEqual(store.get_setting("trading_mode"), "autonomous")
        store.set_setting("trading_mode", "manual")           # ON CONFLICT update
        self.assertEqual(store.get_setting("trading_mode"), "manual")


class TestChat(unittest.TestCase):
    def test_grounded_answer_when_available(self):
        with mock.patch("modules.harness.chat.build_context", return_value={"decision": {}}), \
             mock.patch("modules.harness.agents.available", return_value=True), \
             mock.patch("modules.harness.agents.claude_cli", return_value="Grounded reply."):
            res = chat.answer("what are my best setups?")
        self.assertEqual(res["answer"], "Grounded reply.")
        self.assertTrue(res["llm_used"] and res["grounded"])

    def test_failsoft_when_llm_unavailable(self):
        with mock.patch("modules.harness.agents.available", return_value=False):
            res = chat.answer("anything")
        self.assertFalse(res["llm_used"])
        self.assertIn("isn't available", res["answer"])

    def test_empty_message(self):
        res = chat.answer("   ")
        self.assertFalse(res["llm_used"])


class TestSuggestCache(unittest.TestCase):
    def setUp(self):
        picks._SUGGEST_CACHE.update(key=None, at=0.0, result=None)

    def test_second_call_uses_cache(self):
        calls = {"n": 0}

        def fake_rows(symbols):
            calls["n"] += 1
            return {"AAA": {"symbol": "AAA", "close": 100.0, "atr14": 2.0,
                            "flag": "bull", "rs_1m_pct": 3.0, "rs_3m_pct": 4.0}}

        with mock.patch("modules.harness.picks._rows", side_effect=fake_rows), \
             mock.patch("modules.harness.picks._market_ctx",
                        return_value={"regime": "HEALTHY", "regime_factor": 1.0,
                                      "event_risk": False}):
            r1 = picks.suggest(["AAA"])
            r2 = picks.suggest(["AAA"])
        self.assertEqual(calls["n"], 1)                       # second call hit the cache
        self.assertIs(r1, r2)

    def test_use_cache_false_recomputes(self):
        calls = {"n": 0}

        def fake_rows(symbols):
            calls["n"] += 1
            return {"AAA": {"symbol": "AAA", "close": 100.0, "atr14": 2.0}}

        with mock.patch("modules.harness.picks._rows", side_effect=fake_rows), \
             mock.patch("modules.harness.picks._market_ctx",
                        return_value={"regime": "HEALTHY", "regime_factor": 1.0}):
            picks.suggest(["AAA"])
            picks.suggest(["AAA"], use_cache=False)
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
