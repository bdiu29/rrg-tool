"""Unit tests for the Research (Market Researcher) module — pure helpers plus the
deterministic evidence assembly + template primer over faked module sources. No
network, no LLM: every cross-module reach is patched, and narration is forced to
the deterministic template (llm=False)."""

import contextlib
import unittest
from unittest import mock

import pandas as pd

from modules import research as rs


class TestPureHelpers(unittest.TestCase):
    def test_safe(self):
        self.assertIsNone(rs._safe(float("nan")))
        self.assertIsNone(rs._safe(float("inf")))
        self.assertIsNone(rs._safe(None))
        self.assertEqual(rs._safe(3), 3)
        self.assertEqual(rs._safe("A"), "A")
        self.assertEqual(rs._safe(1.23456), 1.2346)

    def test_thesis_hook_uses_available_fields(self):
        comps = {"pct_from_52w_high": -1.0, "ad_rating": "A", "eps_growth_q": 0.4}
        stats = {"bull_winrate": 64.0, "bull_n": 11, "exhaustion": "seller"}
        hook = rs._thesis_hook(1, comps, stats)
        self.assertIn("#1 by RS", hook)
        self.assertIn("at 52w highs", hook)
        self.assertIn("A accumulation", hook)
        self.assertIn("bull flag 64", hook)
        self.assertIn("bottoming", hook)

    def test_thesis_hook_falls_back_when_empty(self):
        self.assertIn("relative strength", rs._thesis_hook(None, {}, {}))

    def test_list_targets_has_sectors(self):
        # sectors come from the real (constant) SECTOR_NAMES — no network
        t = rs.list_targets()
        ids = {s["id"] for s in t["sectors"]}
        self.assertIn("XLK", ids)
        self.assertEqual(len(t["sectors"]), 11)


# ---------------------------------------------------------------------------
# Evidence assembly + template primer (sources patched)
# ---------------------------------------------------------------------------

def _patches(**over):
    """ExitStack of patches over every cross-module reach, with sensible defaults
    that any test can override by name."""
    snap = pd.DataFrame(
        {"close": [190.0], "chg_pct": [1.2], "rs_1m_pct": [4.0], "rs_3m_pct": [9.0],
         "rsi14": [61.0], "pct_from_52w_high": [-2.0], "ad_rating": ["A"]},
        index=["AAPL"])
    fund = pd.DataFrame(
        {"market_cap": [3.0e12], "pe_ratio": [28.0],
         "eps_growth_q": [0.22], "eps_growth_a": [0.18]},
        index=["AAPL"])
    defaults = {
        "modules.rankings.compute_rankings":
            lambda *a, **k: {"sectors": [{"ticker": "XLK", "rank": 92, "rank_1w": 88,
                                          "rank_1m": 80, "rs_day": 0.3, "rs_wk": 1.1,
                                          "rs_mth": 2.0, "pct_52w_high": -1.5}]},
        "modules.rrg.compute_rrg":
            lambda *a, **k: {"sectors": {"XLK": {"call": "ROTATE IN", "conviction": 55,
                             "quadrant": "Leading", "trend": "up", "wave_label": "wave-3",
                             "why": "golden pocket"}},
                             "regime": "HEALTHY", "rotation": "on"},
        "modules.screener.store.fetch_sector_leaders":
            lambda etf, n: [{"symbol": "AAPL", "close": 190.0, "chg_pct": 1.2, "rs_1m_pct": 4.0},
                            {"symbol": "MSFT", "close": 410.0, "chg_pct": 0.5, "rs_1m_pct": 3.0}],
        "modules.screener.store.get_snapshot": lambda: snap,
        "modules.screener.store.get_fundamentals": lambda: fund,
        "modules.rankings.flag_stats_for":
            lambda syms: {"AAPL": {"bull_winrate": 64.0, "bull_n": 11, "exhaustion": "seller"}},
        "modules.rankings.fetch_holdings":
            lambda etf, n=15: [{"symbol": "AAPL", "name": "Apple", "weight": 22.0}],
        "modules.macro.build_dashboard":
            lambda force=False: {"regime": {"regime": "Goldilocks", "confidence": 61,
                                            "shift_risk": "Low", "playbook": "stay long quality"}},
        "modules.news.calendar.event_risk":
            lambda *a, **k: {"flag": False, "event": None, "note": ""},
    }
    defaults.update(over)
    stack = contextlib.ExitStack()
    for target, fn in defaults.items():
        stack.enter_context(mock.patch(target, fn))
    # never touch a real `claude` binary
    stack.enter_context(mock.patch.object(rs, "available", lambda: False))
    return stack


class TestSectorEvidence(unittest.TestCase):
    def test_build_sector_primer(self):
        with _patches():
            p = rs.build_research("sector", "XLK", angle="rotation read", llm=False)
        self.assertEqual(p["type"], "sector")
        self.assertEqual(p["id"], "XLK")
        # overview folded the rank + the rotation call + the macro regime
        self.assertEqual(p["overview"]["rank"]["rank"], 92)
        self.assertEqual(p["overview"]["rotation_call"]["call"], "ROTATE IN")
        self.assertEqual(p["overview"]["macro_regime"]["regime"], "Goldilocks")
        self.assertEqual(p["overview"]["breadth_regime"], "HEALTHY")
        # competitive landscape + comps + ideas
        self.assertTrue(p["competitive_landscape"]["leaders"])
        self.assertEqual(p["competitive_landscape"]["leaders"][0]["bull_winrate"], 64.0)
        self.assertIn("AAPL", p["peer_comps"])
        self.assertEqual(p["peer_comps"]["AAPL"]["pe_ratio"], 28.0)
        syms = [i["symbol"] for i in p["ideas"]]
        self.assertEqual(syms[0], "AAPL")
        # template primer (no LLM) mentions the name + an idea
        self.assertFalse(p["llm_used"])
        self.assertIn("XLK", p["primer"])
        self.assertIn("AAPL", p["primer"])

    def test_failsoft_when_a_source_raises(self):
        def boom(*a, **k):
            raise RuntimeError("rankings down")
        with _patches(**{"modules.rankings.compute_rankings": boom}):
            p = rs.build_research("sector", "XLK", llm=False)
        # rank section degrades to empty but the primer still renders
        self.assertEqual(p["overview"]["rank"], {})
        self.assertIn("XLK", p["primer"])
        self.assertTrue(p["competitive_landscape"]["leaders"])   # other sections survived

    def test_comps_empty_when_no_snapshot(self):
        with _patches(**{"modules.screener.store.get_snapshot": lambda: None}):
            p = rs.build_research("sector", "XLK", llm=False)
        self.assertEqual(p["peer_comps"], {})
        # ideas still come from the leaders, just without comps fields
        self.assertTrue(p["ideas"])


class TestSummary(unittest.TestCase):
    def test_summary_failsoft(self):
        def boom(*a, **k):
            raise RuntimeError("down")
        with mock.patch("modules.rankings.compute_rankings", boom):
            resp = rs._handle_summary(None)
        self.assertEqual(resp.status, 200)
        self.assertIn("no data", resp.body)


# ---------------------------------------------------------------------------
# Per-ticker fundamental score (pure) + the standalone analyze path
# ---------------------------------------------------------------------------

class TestFundamentalScore(unittest.TestCase):
    def test_growth_frac_tolerates_fraction_or_percent(self):
        self.assertAlmostEqual(rs._growth_frac(0.25), 0.25)   # fraction (snapshot)
        self.assertAlmostEqual(rs._growth_frac(25.0), 0.25)   # percent (yfinance .info)
        self.assertIsNone(rs._growth_frac(None))

    def test_valuation_bands(self):
        self.assertEqual(rs._fs_valuation(15), 90.0)          # cheap/reasonable
        self.assertEqual(rs._fs_valuation(70), 15.0)          # very rich
        self.assertEqual(rs._fs_valuation(-5), 35.0)          # no earnings
        self.assertIsNone(rs._fs_valuation(None))
        mid = rs._fs_valuation(40)
        self.assertTrue(15 < mid < 90)

    def test_inst_score_unit_tolerant(self):
        self.assertEqual(rs._fs_institutional(0.60), 80.0)    # fraction, healthy band
        self.assertEqual(rs._fs_institutional(60), 80.0)      # percent, same band
        self.assertEqual(rs._fs_institutional(0.05), 35.0)    # no sponsorship
        self.assertIsNone(rs._fs_institutional(None))

    def test_score_blends_present_subscores(self):
        row = {"eps_growth_q": 0.30, "eps_growth_a": 0.20, "ad_rating": "A",
               "inst_pct_held": 0.7, "pe_ratio": 22, "close": 100, "sma50": 90,
               "sma200": 80, "pct_from_52w_high": -2, "rsi14": 60,
               "rs_1m_pct": 4.0, "rs_3m_pct": 8.0}
        fs = rs.fundamental_score(row)
        self.assertIsNotNone(fs)
        self.assertTrue(0 <= fs["score"] <= 99)
        self.assertEqual(fs["verdict"], rs._verdict(fs["score"]))
        self.assertEqual(fs["n_inputs"], 6)                   # every sub-score present
        # a clearly strong name lands high
        self.assertGreater(fs["score"], 65)

    def test_score_none_when_nothing_scorable(self):
        self.assertIsNone(rs.fundamental_score({"symbol": "X"}))

    def test_analyze_ticker_failsoft_when_unresolvable(self):
        # picks._rows returns {} (couldn't price) → a graceful payload, no crash
        with mock.patch("modules.harness.picks._rows", lambda syms: {}), \
             mock.patch.object(rs, "available", lambda: False):
            p = rs.build_research("ticker", "ZZZZ", llm=False)
        self.assertEqual(p["type"], "ticker")
        self.assertIsNone(p["fundamental"])
        self.assertIn("ZZZZ", p["primer"])

    def test_analyze_ticker_scores_a_resolved_row(self):
        row = {"symbol": "NVDA", "eps_growth_q": 0.5, "eps_growth_a": 0.4, "ad_rating": "A",
               "inst_pct_held": 0.65, "pe_ratio": 30, "close": 120, "sma50": 100,
               "sma200": 80, "pct_from_52w_high": -3, "rsi14": 62, "rs_1m_pct": 6.0,
               "rs_3m_pct": 12.0, "sector": "Technology", "_source": "on-demand"}
        with mock.patch("modules.harness.picks._rows", lambda syms: {"NVDA": row}), \
             mock.patch("modules.rrg.compute_rrg", lambda *a, **k: {
                 "sectors": {"XLK": {"call": "ROTATE IN", "conviction": 60}},
                 "regime": "HEALTHY", "rotation": "on"}), \
             mock.patch("modules.schwab.SECTOR_ETF_MAP", {"Technology": "XLK"}), \
             mock.patch("modules.macro.build_dashboard",
                        lambda force=False: {"regime": {"regime": "Goldilocks", "confidence": 61}}), \
             mock.patch("modules.news.calendar.event_risk",
                        lambda *a, **k: {"flag": False, "note": ""}), \
             mock.patch.object(rs, "available", lambda: False):
            p = rs.build_research("ticker", "NVDA", llm=False)
        self.assertEqual(p["fundamental"]["verdict"], "Strong")
        self.assertEqual(p["context"]["sector"]["call"], "ROTATE IN")
        self.assertEqual(p["context"]["sector"]["etf"], "XLK")
        self.assertIn("NVDA", p["primer"])


class TestPicksHoldBlend(unittest.TestCase):
    """The fundamental score wires into picks.py's HOLD axis."""
    def test_fund_score_raises_hold(self):
        from modules.harness import picks
        # a row with a weak CANSLIM proxy (no fundamentals) but no fund score
        row = {"symbol": "AAA", "close": 100, "sma50": 95, "sma200": 90,
               "rs_1m_pct": 1.0, "_rs_rating": 50}
        base, _, _ = picks._hold(dict(row))
        # same row but with a high attached fundamental score → HOLD lifts
        boosted, why, _ = picks._hold({**row, "_fund_score": 90})
        self.assertGreater(boosted, base)
        self.assertTrue(any("fundamentals" in w for w in why))

    def test_attach_fundamentals_populates_rows(self):
        from modules.harness import picks
        rows = {"AAA": {"symbol": "AAA", "eps_growth_q": 0.3, "ad_rating": "A",
                        "pe_ratio": 18, "rs_1m_pct": 5.0, "rs_3m_pct": 9.0,
                        "close": 50, "sma50": 45, "sma200": 40, "pct_from_52w_high": -4}}
        picks._attach_fundamentals(rows)
        self.assertIsNotNone(rows["AAA"]["_fund_score"])
        self.assertIn(rows["AAA"]["_fund_verdict"], ("Strong", "Solid", "Mixed", "Weak"))


if __name__ == "__main__":
    unittest.main()
