"""Unit tests for the watchlist parser + the impulse×hold-ability suggestion engine
(Phase 3 / Layer A). No network — `score_symbol` is pure and `suggest`'s data fetch
is monkeypatched."""

import unittest
from unittest import mock

from modules.harness import watchlist, picks


# ---------------------------------------------------------------------------
# TradingView export parsing
# ---------------------------------------------------------------------------

class TestParseWatchlist(unittest.TestCase):
    def test_tradingview_txt_with_sections(self):
        text = "NASDAQ:AAPL,NYSE:BRK.B,###Tech,NASDAQ:MSFT"
        self.assertEqual(watchlist.parse_watchlist(text), ["AAPL", "BRK-B", "MSFT"])

    def test_csv_with_header_column(self):
        text = "Ticker,Last,Chg%\nAAPL,150,1.2\nMSFT,400,-0.3"
        self.assertEqual(watchlist.parse_watchlist(text), ["AAPL", "MSFT"])

    def test_plain_list_and_dedupe(self):
        self.assertEqual(watchlist.parse_watchlist("AAPL\nMSFT\nAAPL\nGOOGL"),
                         ["AAPL", "MSFT", "GOOGL"])

    def test_dot_class_share_translated(self):
        self.assertEqual(watchlist.parse_watchlist("BRK.B, BF.B"), ["BRK-B", "BF-B"])

    def test_drops_section_headers_and_blanks(self):
        self.assertEqual(watchlist.parse_watchlist("###Watchlist\n\nAAPL,,MSFT,"),
                         ["AAPL", "MSFT"])

    def test_empty(self):
        self.assertEqual(watchlist.parse_watchlist(""), [])


# ---------------------------------------------------------------------------
# score_symbol — the two axes + the gate
# ---------------------------------------------------------------------------

def _strong_quality_row():
    """A clean breakout on a high-quality, liquid name."""
    return {
        "symbol": "GOOD", "close": 100.0, "atr14": 3.0,
        "flag": "bull", "gp_in_pocket": 1.0, "rvol_10d": 3.2, "ad_rating": "A",
        "rs_1m_pct": 5.0, "rs_3m_pct": 9.0, "ema20": 95.0, "ema50": 90.0,
        "sma50": 92.0, "sma200": 80.0, "avg_vol_10d": 2_000_000.0,
        "pct_from_52w_high": -1.0, "high_20d": 98.0,
        "eps_growth_q": 40.0, "eps_growth_a": 30.0, "shares_outstanding": 1e8,
        "_rs_rating": 92.0, "_float_pctl": 60.0,
    }


class TestScoreSymbol(unittest.TestCase):
    def test_strong_setup_quality_is_tradeable(self):
        s = picks.score_symbol(_strong_quality_row(), {"regime_factor": 1.0})
        self.assertGreaterEqual(s["impulse"], picks.MIN_IMPULSE)
        self.assertGreaterEqual(s["hold"], picks.MIN_HOLD)
        self.assertTrue(s["tradeable"])
        self.assertEqual(s["stop"], 94.0)            # 100 − 2×3
        self.assertEqual(s["target"], 112.0)
        self.assertEqual(s["risk_pct"], 6.0)

    def test_junk_penny_setup_fails_hold_gate(self):
        row = _strong_quality_row()
        row.update(symbol="JUNK", close=2.0, avg_vol_10d=100_000.0,  # penny + thin
                   eps_growth_q=None, eps_growth_a=None)
        s = picks.score_symbol(row, {"regime_factor": 1.0})
        self.assertGreaterEqual(s["impulse"], picks.MIN_IMPULSE)     # great setup
        self.assertLess(s["hold"], picks.MIN_HOLD)                   # but not holdable
        self.assertFalse(s["tradeable"])
        self.assertEqual(s["reason"], "quality too low to hold")

    def test_quality_name_no_setup_not_a_buy(self):
        row = _strong_quality_row()
        row.update(symbol="DULL", flag="none", gp_in_pocket=0.0, gp_approaching=0.0,
                   rvol_10d=1.0, ad_rating="C", rs_1m_pct=-1.0, rs_3m_pct=1.0,
                   ema20=101.0, pct_from_52w_high=-18.0, high_20d=110.0)
        s = picks.score_symbol(row, {"regime_factor": 1.0})
        self.assertLess(s["impulse"], picks.MIN_IMPULSE)
        self.assertFalse(s["tradeable"])
        self.assertEqual(s["reason"], "no actionable setup yet")

    def test_event_risk_dampens(self):
        base = picks.score_symbol(_strong_quality_row(), {"regime_factor": 1.0})
        damp = picks.score_symbol(_strong_quality_row(),
                                  {"regime_factor": 1.0, "event_risk": True})
        self.assertLess(damp["pick"], base["pick"])
        self.assertTrue(any("event/earnings" in w for w in damp["why"]))

    def test_regime_factor_scales_pick(self):
        full = picks.score_symbol(_strong_quality_row(), {"regime_factor": 1.0})
        weak = picks.score_symbol(_strong_quality_row(), {"regime_factor": 0.75})
        self.assertLess(weak["pick"], full["pick"])


# ---------------------------------------------------------------------------
# suggest — ranking (data fetch injected)
# ---------------------------------------------------------------------------

class TestSuggest(unittest.TestCase):
    def test_ranks_by_pick_desc(self):
        rows = {
            "GOOD": _strong_quality_row(),
            "DULL": {**_strong_quality_row(), "symbol": "DULL", "flag": "none",
                     "gp_in_pocket": 0.0, "rvol_10d": 1.0, "ad_rating": "C",
                     "rs_1m_pct": 0.5, "pct_from_52w_high": -20.0, "high_20d": 130.0,
                     "ema20": 101.0},
        }
        with mock.patch("modules.harness.picks._rows", return_value=rows), \
             mock.patch("modules.harness.picks._market_ctx",
                        return_value={"regime": "HEALTHY", "regime_factor": 1.0,
                                      "event_risk": False}):
            rep = picks.suggest(["GOOD", "DULL"])
        self.assertEqual(rep["count"], 2)
        self.assertEqual(rep["suggestions"][0]["symbol"], "GOOD")   # higher pick first
        self.assertGreaterEqual(rep["suggestions"][0]["pick"], rep["suggestions"][1]["pick"])

    def test_empty_watchlist(self):
        with mock.patch("modules.harness.picks._rows", return_value={}):
            rep = picks.suggest([])
        self.assertEqual(rep["count"], 0)
        self.assertEqual(rep["suggestions"], [])


if __name__ == "__main__":
    unittest.main()
