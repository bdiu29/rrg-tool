"""
Unit tests for the sector rankings math.

Pins the rank pipeline: an RS composite (relative-strength returns vs SPY),
mapped to a 0-99 *pooled historical percentile*, with rank-up/down movers
derived from the change in that percentile. The integration test mocks the
shared price-fetch path so no network is touched.

Run:  /usr/bin/python3 -m unittest discover tests
"""

import unittest
from unittest import mock

import numpy as np
import pandas as pd

from modules import rankings
from modules.rrg import signal


def _synthetic_panel():
    """300 business days: XLK clearly beats SPY, XLU lags, XLP ~ tracks it."""
    i = np.arange(300)
    idx = pd.bdate_range("2022-01-03", periods=300)
    return pd.DataFrame({
        "XLK": 100 * np.exp(0.0008 * i),    # outperformer  → rs rising
        "XLU": 100 * np.exp(0.0000 * i),    # laggard       → rs falling vs SPY
        "XLP": 100 * np.exp(0.00031 * i),   # ~ matches SPY
        "SPY": 100 * np.exp(0.0003 * i),
    }, index=idx)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestPercentileMapper(unittest.TestCase):
    def test_monotonic_and_bounded(self):
        ref = np.arange(0.0, 100.0)
        pctl = rankings._percentile_mapper(ref)
        self.assertEqual(pctl(-10), 0)          # below everything
        self.assertEqual(pctl(1e9), 99)         # above everything → capped at 99
        self.assertTrue(0 <= pctl(50) <= 99)
        self.assertGreater(pctl(80), pctl(20))  # order preserved

    def test_none_and_nan_pass_through(self):
        pctl = rankings._percentile_mapper(np.array([1.0, 2.0, 3.0]))
        self.assertIsNone(pctl(None))
        self.assertIsNone(pctl(float("nan")))

    def test_empty_reference_yields_none(self):
        pctl = rankings._percentile_mapper(np.array([np.nan, np.nan]))
        self.assertIsNone(pctl(1.0))


class TestRetRel(unittest.TestCase):
    def test_ret_basic(self):
        s = pd.Series([100.0, 110.0, 121.0])
        self.assertAlmostEqual(rankings._ret(s, 1), 10.0)     # 121→from 110
        self.assertAlmostEqual(rankings._ret(s, 2), 21.0)     # 121 from 100

    def test_ret_too_short(self):
        self.assertIsNone(rankings._ret(pd.Series([100.0]), 1))

    def test_rel(self):
        self.assertEqual(rankings._rel(3.0, 1.0), 2.0)
        self.assertIsNone(rankings._rel(None, 1.0))
        self.assertIsNone(rankings._rel(1.0, None))


class TestRsComposite(unittest.TestCase):
    def test_outperformer_positive_laggard_negative(self):
        panel = _synthetic_panel()
        comp = rankings._rs_composite(panel, ["XLK", "XLU", "XLP"], "SPY")
        self.assertGreater(comp["XLK"].dropna().iloc[-1], 0)
        self.assertLess(comp["XLU"].dropna().iloc[-1], 0)
        # the strong sector's composite dominates the laggard's everywhere late
        self.assertGreater(comp["XLK"].dropna().iloc[-1],
                           comp["XLP"].dropna().iloc[-1])


class TestRankMovers(unittest.TestCase):
    def _sectors(self):
        return [
            {"ticker": "A", "name": "A", "rank": 90, "rank_1d": 40, "price": 1.0},
            {"ticker": "B", "name": "B", "rank": 60, "rank_1d": 65, "price": 1.0},
            {"ticker": "C", "name": "C", "rank": 50, "rank_1d": 50, "price": 1.0},
            {"ticker": "D", "name": "D", "rank": 10, "rank_1d": 80, "price": 1.0},
            {"ticker": "E", "name": "E", "rank": None, "rank_1d": 5, "price": 1.0},
        ]

    def test_ups_downs_ordering(self):
        ups, downs = rankings._rank_movers(self._sectors(), "rank_1d", n=5)
        # only A rose (+50); B (-5) and D (-70) fell; C flat and E null excluded
        self.assertEqual([m["ticker"] for m in ups], ["A"])
        self.assertEqual(ups[0]["delta"], 50)
        # downs sorted most-negative first
        self.assertEqual([m["ticker"] for m in downs], ["D", "B"])
        self.assertEqual(downs[0]["delta"], -70)


# ---------------------------------------------------------------------------
# Integration — compute_rankings over a mocked price panel
# ---------------------------------------------------------------------------

class TestComputeRankings(unittest.TestCase):
    def test_leaderboard_orders_by_strength(self):
        with mock.patch.object(signal, "_fetch_close", return_value=_synthetic_panel()):
            res = rankings.compute_rankings(tickers=["XLK", "XLU", "XLP"],
                                            benchmark="SPY")
        tickers = [s["ticker"] for s in res["sectors"]]
        self.assertEqual(tickers[0], "XLK")     # strongest sorts first
        self.assertEqual(tickers[-1], "XLU")    # laggard last

        ranks = {s["ticker"]: s["rank"] for s in res["sectors"]}
        for v in ranks.values():
            self.assertTrue(0 <= v <= 99)
        self.assertGreater(ranks["XLK"], ranks["XLP"])
        self.assertGreater(ranks["XLP"], ranks["XLU"])
        self.assertNotEqual(ranks["XLK"], ranks["XLU"])   # continuous, not a tie

    def test_row_shape_and_signs(self):
        with mock.patch.object(signal, "_fetch_close", return_value=_synthetic_panel()):
            res = rankings.compute_rankings(tickers=["XLK", "XLU", "XLP"],
                                            benchmark="SPY")
        self.assertIsNotNone(res["date"])
        row = next(s for s in res["sectors"] if s["ticker"] == "XLK")
        for k in ("rank", "rank_1d", "rank_1w", "rank_1m", "price",
                  "rs_day", "rs_wk", "rs_mth", "pct_52w_high"):
            self.assertIn(k, row)
        self.assertGreater(row["rs_mth"], 0)            # XLK beats SPY over a month
        lag = next(s for s in res["sectors"] if s["ticker"] == "XLU")
        self.assertLess(lag["rs_mth"], 0)               # XLU trails SPY
        self.assertLessEqual(row["pct_52w_high"], 0.01)  # monotonic ⇒ ~at its high


class TestHoldings(unittest.TestCase):
    def setUp(self):
        rankings._HOLDINGS_CACHE.clear()

    def _fake_ticker(self):
        th = pd.DataFrame(
            {"Name": ["NVIDIA Corp", "Apple Inc"], "Holding Percent": [0.1307, 0.1167]},
            index=pd.Index(["NVDA", "AAPL"], name="Symbol"),
        )
        fake = mock.Mock()
        fake.funds_data.top_holdings = th
        return fake

    def test_weights_scaled_enriched_and_cached(self):
        with mock.patch("yfinance.Ticker", return_value=self._fake_ticker()), \
             mock.patch("modules.screener.store.fetch_quotes",
                        return_value={"NVDA": {"close": 100.0, "chg_pct": 1.2,
                                               "rs_1m_pct": 5.0}}):
            out = rankings.fetch_holdings("XLK")
        self.assertEqual(out[0]["symbol"], "NVDA")
        self.assertAlmostEqual(out[0]["weight"], 13.07)     # fraction → percent
        self.assertEqual(out[0]["close"], 100.0)            # enriched from snapshot
        self.assertEqual(out[1]["symbol"], "AAPL")
        self.assertIsNone(out[1]["close"])                  # not in snapshot → None
        self.assertIn("XLK", rankings._HOLDINGS_CACHE)      # success is cached

    def test_fetch_failure_returns_empty_uncached(self):
        with mock.patch("yfinance.Ticker", side_effect=RuntimeError("boom")):
            out = rankings.fetch_holdings("XLE")
        self.assertEqual(out, [])
        self.assertNotIn("XLE", rankings._HOLDINGS_CACHE)   # failures not cached


if __name__ == "__main__":
    unittest.main()
