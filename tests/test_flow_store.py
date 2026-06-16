"""Integration tests for the flow store + poller pass (hermetic: temp DB, fake
source, no network)."""

import tempfile
import unittest
from pathlib import Path

from modules.flow import store, poller, source


def _conviction_contract():
    return dict(option_symbol="AAPL_C200", underlying="AAPL", put_call="CALL",
                strike=200.0, expiry="2026-08-21", dte=45, spot=190.0,
                session_volume=20000, open_interest=500, bid=2.0, ask=2.1,
                last=2.1, mark=2.05, ts="2026-06-15 10:00:00")


def _noise_contract():
    return dict(option_symbol="AAPL_C999", underlying="AAPL", put_call="CALL",
                strike=999.0, expiry="2026-06-16", dte=1, spot=190.0,
                session_volume=10, open_interest=5000, bid=0.01, ask=0.02,
                last=0.02, mark=0.02, ts="2026-06-15 10:00:00")


class FakeSource:
    def capabilities(self):
        return {"source": "fake", "tier": "snapshot", "aggressor": "estimated"}

    def get_chain(self, symbol):
        return [_conviction_contract(), _noise_contract()] if symbol == "AAPL" else []


class FlowDBTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        store._DB_PATH = Path(self._tmp.name)
        store._INITED = False
        store.init_db()

    def tearDown(self):
        Path(self._tmp.name).unlink(missing_ok=True)


class TestClusterAndBaseline(FlowDBTest):
    def test_cluster_increments_on_bursts_then_resets_next_day(self):
        c = _conviction_contract()
        vd, cl = store.record_poll(c, "2026-06-15", 100_000)
        self.assertEqual((vd, cl), (20000, 1))                # first poll = one burst
        c["session_volume"] = 20400                            # +400 contracts ≈ $82k < $100k → no burst
        _vd, cl = store.record_poll(c, "2026-06-15", 100_000)
        self.assertEqual(cl, 1)
        c["session_volume"] = 60000                            # +39000 → a burst
        _vd, cl = store.record_poll(c, "2026-06-15", 100_000)
        self.assertEqual(cl, 2)
        _vd, cl = store.record_poll(c, "2026-06-16", 100_000)  # new day resets
        self.assertEqual(cl, 1)

    def test_baseline_excludes_today(self):
        store.record_ticker_notional("AAPL", "2026-06-12", 1_000_000)
        store.record_ticker_notional("AAPL", "2026-06-13", 3_000_000)
        store.record_ticker_notional("AAPL", "2026-06-15", 9_000_000)   # today
        self.assertEqual(store.ticker_baseline("AAPL", "2026-06-15"), 2_000_000)


class TestAlertDedupe(FlowDBTest):
    def test_alert_fires_once_per_contract_rule_day(self):
        a1 = store.insert_alert("2026-06-15", "AAPL_C200", "flow:conviction", "bull", "m", {})
        a2 = store.insert_alert("2026-06-15", "AAPL_C200", "flow:conviction", "bull", "m", {})
        self.assertIsNotNone(a1)
        self.assertIsNone(a2)


class TestOIConfirmation(FlowDBTest):
    def test_opened_when_oi_jumps(self):
        c, r = _conviction_contract(), {"classification": "conviction", "conviction": 90,
                                        "direction": "bullish", "factors": []}
        store.upsert_flow_signal("2026-06-15", c, r, {})
        store.record_oi("AAPL_C200", "2026-06-15", 500)
        store.record_oi("AAPL_C200", "2026-06-16", 18000)        # OI exploded → opened
        poller.confirm_entries("2026-06-16")
        self.assertEqual(store.get_flow_signal("2026-06-15", "AAPL_C200")["entry_exit"], "opened")

    def test_closed_when_oi_collapses(self):
        c, r = _conviction_contract(), {"classification": "conviction", "conviction": 90,
                                        "direction": "bullish", "factors": []}
        c = dict(c, open_interest=20000)
        store.upsert_flow_signal("2026-06-15", c, r, {})
        store.record_oi("AAPL_C200", "2026-06-16", 1000)          # OI collapsed → closed
        poller.confirm_entries("2026-06-16")
        self.assertEqual(store.get_flow_signal("2026-06-15", "AAPL_C200")["entry_exit"], "closed")


class TestRunPass(FlowDBTest):
    def test_pass_flags_conviction_drops_noise(self):
        store.set_setting("universe", ["AAPL"])
        orig_resolve = poller.source.resolve_source
        orig_focus = poller._focus_list
        orig_ctx = poller.context.build_context
        try:
            poller.source.resolve_source = lambda *a, **k: (FakeSource(), None)
            poller._focus_list = lambda: []
            poller.context.build_context = lambda syms: {}
            n = poller.run_pass()
        finally:
            poller.source.resolve_source = orig_resolve
            poller._focus_list = orig_focus
            poller.context.build_context = orig_ctx

        self.assertEqual(n, 1)                                    # noise dropped, strong flow kept
        feed = store.list_flow_signals(store.latest_signal_date())
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["option_symbol"], "AAPL_C200")
        self.assertIn(feed[0]["classification"], ("notable", "conviction"))
        self.assertGreaterEqual(len(store.list_alerts()), 1)


if __name__ == "__main__":
    unittest.main()
