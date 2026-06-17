"""
News / macro-events module tests — pure logic, no network.

Covers: importance classification, the date-cadence helpers, the FOMC static
table, the no-key approximate econ generator, store dedupe/upsert + filters,
and the event_risk / summary signal hooks (driven through a fake source against
a hermetic temp DB).
"""

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from modules.news import calendar, sources, store


def _d(days):
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


class FakeSource(sources.EventSource):
    name = "fake"

    def __init__(self, events):
        self._events = events

    def fetch(self, start, end):
        out = []
        for e in self._events:
            d = datetime.strptime(e["event_date"], "%Y-%m-%d").date()
            if start <= d <= end:
                out.append(e)
        return out


class ImportanceTest(unittest.TestCase):
    def test_keyword_buckets(self):
        self.assertEqual(sources.classify_importance("Consumer Price Index"), "high")
        self.assertEqual(sources.classify_importance("Employment Situation"), "high")
        self.assertEqual(sources.classify_importance("Producer Price Index"), "med")
        self.assertEqual(sources.classify_importance("Initial Jobless Claims"), "med")
        self.assertEqual(sources.classify_importance("Weekly Bauxite Survey"), "low")


class DateHelpersTest(unittest.TestCase):
    def test_first_friday(self):
        f = sources._first_weekday(2026, 6, 4)     # Fri = weekday 4
        self.assertEqual(f.weekday(), 4)
        self.assertLessEqual(f.day, 7)

    def test_last_business_day(self):
        d = sources._last_business_day(2026, 6)
        self.assertLess(d.weekday(), 5)            # never a weekend
        self.assertGreaterEqual(d.day, 28)

    def test_months_in_window(self):
        got = list(sources._months_in_window(date(2026, 1, 15), date(2026, 3, 2)))
        self.assertEqual(got, [(2026, 1), (2026, 2), (2026, 3)])


class FOMCSourceTest(unittest.TestCase):
    def test_known_meeting_and_minutes(self):
        evs = sources.FOMCSource().fetch(date(2026, 6, 1), date(2026, 7, 15))
        decisions = [e for e in evs if e["kind"] == "fomc"]
        self.assertTrue(any(e["event_date"] == "2026-06-17" for e in decisions))
        for e in decisions:
            self.assertEqual(e["importance"], "high")
        # Minutes ~21 days after the 6/17 meeting land as a fed_news item.
        minutes = [e for e in evs if e["kind"] == "fed_news"]
        self.assertTrue(any(e["event_date"] == "2026-07-08" for e in minutes))


class EconApproxTest(unittest.TestCase):
    def setUp(self):
        self._orig = sources.fred_key
        sources.fred_key = lambda: ""              # force the no-key path

    def tearDown(self):
        sources.fred_key = self._orig

    def test_monthly_prints_generated(self):
        evs = sources.EconCalendarSource().fetch(date(2026, 6, 1), date(2026, 6, 30))
        titles = " ".join(e["title"] for e in evs)
        self.assertIn("Nonfarm", titles)
        self.assertIn("CPI", titles)
        nfp = [e for e in evs if "Nonfarm" in e["title"]][0]
        self.assertEqual(nfp["importance"], "high")
        self.assertEqual(nfp["source"], "econ_approx")
        # NFP is the first Friday — exact even on the no-key path.
        self.assertEqual(datetime.strptime(nfp["event_date"], "%Y-%m-%d").weekday(), 4)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir, self._orig_db = store._DATA_DIR, store.DB_PATH
        store._DATA_DIR = Path(self._tmp.name)
        store.DB_PATH   = Path(self._tmp.name) / "news.db"
        store.init_db()

    def tearDown(self):
        store._DATA_DIR, store.DB_PATH = self._orig_dir, self._orig_db
        self._tmp.cleanup()

    def _ev(self, **kw):
        base = dict(source="fomc", kind="fomc", event_date=_d(1),
                    title="FOMC rate decision", detail="v1", importance="high")
        base.update(kw)
        return base

    def test_dedupe_and_update(self):
        store.upsert_events([self._ev()])
        store.upsert_events([self._ev(detail="v2")])   # same key → update, not dup
        rows = store.get_events(_d(0), _d(5))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["detail"], "v2")

    def test_importance_and_kind_filters(self):
        store.upsert_events([
            self._ev(),
            self._ev(kind="econ", title="CPI", importance="high"),
            self._ev(kind="fed_speech", title="Powell speaks", importance="med"),
        ])
        highs = store.get_events(_d(0), _d(5), importance=["high"])
        self.assertEqual(len(highs), 2)
        econ = store.get_events(_d(0), _d(5), kinds=["econ"])
        self.assertEqual([e["title"] for e in econ], ["CPI"])


class SignalHookTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir, self._orig_db = store._DATA_DIR, store.DB_PATH
        self._orig_sources, self._orig_refresh = sources.ALL_SOURCES, calendar._last_refresh
        store._DATA_DIR = Path(self._tmp.name)
        store.DB_PATH   = Path(self._tmp.name) / "news.db"
        store.init_db()
        calendar._last_refresh = None

    def tearDown(self):
        store._DATA_DIR, store.DB_PATH = self._orig_dir, self._orig_db
        sources.ALL_SOURCES = self._orig_sources
        calendar._last_refresh = self._orig_refresh
        self._tmp.cleanup()

    def _seed(self, events):
        sources.ALL_SOURCES = [FakeSource(events)]
        calendar._last_refresh = None

    def test_event_risk_flags_imminent(self):
        self._seed([
            dict(source="fomc", kind="fomc", event_date=_d(2), title="FOMC rate decision",
                 importance="high", detail=None, event_time=None, symbols=None, url=None),
            dict(source="fred", kind="econ", event_date=_d(5), title="CPI",
                 importance="high", detail=None, event_time=None, symbols=None, url=None),
        ])
        er = calendar.event_risk(horizon=10, alert_days=2)
        self.assertTrue(er["flag"])
        self.assertEqual(er["event"]["event_date"], _d(2))   # soonest wins
        self.assertIn("event risk", er["note"])

    def test_event_risk_none_when_only_minor(self):
        self._seed([
            dict(source="fed_rss", kind="fed_speech", event_date=_d(1), title="Powell speaks",
                 importance="med", detail=None, event_time=None, symbols=None, url=None),
        ])
        er = calendar.event_risk(horizon=10, alert_days=2)
        self.assertFalse(er["flag"])
        self.assertIsNone(er["event"])

    def test_summary_text(self):
        self._seed([
            dict(source="fomc", kind="fomc", event_date=_d(1), title="FOMC rate decision",
                 importance="high", detail=None, event_time=None, symbols=None, url=None),
        ])
        s = calendar.summary()
        self.assertIn("FOMC", s["text"])
        self.assertIn("tomorrow", s["text"])
        self.assertTrue(s["flag"])
        self.assertEqual(s["status"], "accent")


class FormatHelpersTest(unittest.TestCase):
    def test_fmt_time(self):
        self.assertEqual(calendar._fmt_time("08:30"), "8:30 am")
        self.assertEqual(calendar._fmt_time("14:00"), "2:00 pm")
        self.assertEqual(calendar._fmt_time("00:15"), "12:15 am")
        self.assertEqual(calendar._fmt_time(None), "")

    def test_econ_period(self):
        cpi = dict(kind="econ", title="Consumer Price Index (CPI)", event_date="2026-06-12")
        self.assertEqual(calendar._econ_period(cpi), "May")          # monthly → prior month
        gdp = dict(kind="econ", title="Gross Domestic Product (GDP)", event_date="2026-07-28")
        self.assertEqual(calendar._econ_period(gdp), "Q2 2026")      # quarterly → prior quarter
        fomc = dict(kind="fomc", title="FOMC rate decision", event_date="2026-06-17")
        self.assertEqual(calendar._econ_period(fomc), "")           # no data period


class FredValuesTest(unittest.TestCase):
    def test_match_series(self):
        self.assertEqual(sources.match_series("Consumer Price Index (CPI)")[0], "CPIAUCSL")
        self.assertEqual(sources.match_series("Core CPI")[0], "CPILFESL")   # core before generic
        self.assertEqual(sources.match_series("Employment Situation (Nonfarm Payrolls)")[0], "PAYEMS")
        self.assertIsNone(sources.match_series("NFIB optimism index"))

    def test_format_value(self):
        self.assertEqual(sources.format_econ_value(0.5, "pct"), "0.5%")
        self.assertEqual(sources.format_econ_value(-0.1, "pct"), "-0.1%")
        self.assertEqual(sources.format_econ_value(139.0, "payrolls"), "+139,000")
        self.assertEqual(sources.format_econ_value(220.0, "count"), "220")
        self.assertEqual(sources.format_econ_value(None, "pct"), "")

    def test_actual_previous_alignment(self):
        self._orig_k, self._orig_o = sources.fred_key, sources.fred_observations
        sources.fred_key = lambda: "x"
        sources.fred_observations = lambda s, u: (
            {"2026-05-01": 0.5, "2026-04-01": 0.6} if s == "CPIAUCSL" else {})
        try:
            # Released June reports May → Actual = May (0.5%), Previous = April (0.6%).
            a, p = sources.econ_actual_previous("Consumer Price Index (CPI)", "2026-06-12")
            self.assertEqual((a, p), ("0.5%", "0.6%"))
            # Upcoming July release reports June (not yet in FRED) → Actual blank, Previous = May.
            a2, p2 = sources.econ_actual_previous("Consumer Price Index (CPI)", "2026-07-13")
            self.assertEqual((a2, p2), ("", "0.5%"))
            # Unmapped report → both blank, no fetch.
            self.assertEqual(sources.econ_actual_previous("NFIB optimism index", "2026-06-09"), ("", ""))
        finally:
            sources.fred_key, sources.fred_observations = self._orig_k, self._orig_o


class WeekCalendarTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir, self._orig_db = store._DATA_DIR, store.DB_PATH
        self._orig_sources, self._orig_refresh = sources.ALL_SOURCES, calendar._last_refresh
        store._DATA_DIR = Path(self._tmp.name)
        store.DB_PATH   = Path(self._tmp.name) / "news.db"
        store.init_db()
        # Seed an econ event Monday-of-this-week and an earnings event that Wednesday.
        monday_off = -date.today().weekday()
        sources.ALL_SOURCES = [FakeSource([
            dict(source="econ_approx", kind="econ", event_date=_d(monday_off),
                 title="Consumer Price Index (CPI)", importance="high",
                 event_time="08:30", detail=None, symbols=None, url=None, extra=None),
            dict(source="finnhub", kind="earnings", event_date=_d(monday_off + 2),
                 title="AAPL", importance="med", event_time=None, detail=None,
                 symbols="AAPL", url=None,
                 extra=json.dumps({"when": "amc", "eps_est": 1.5, "eps_actual": None,
                                   "period": "Q3 2026"})),
        ])]
        calendar._last_refresh = None

    def tearDown(self):
        store._DATA_DIR, store.DB_PATH = self._orig_dir, self._orig_db
        sources.ALL_SOURCES = self._orig_sources
        calendar._last_refresh = self._orig_refresh
        self._tmp.cleanup()

    def test_econ_track_structure(self):
        cal = calendar.build_week_calendar(track="econ", weeks=2)
        self.assertEqual(cal["track"], "econ")
        self.assertEqual(len(cal["weeks"]), 2)
        for wk in cal["weeks"]:
            self.assertEqual(len(wk["days"]), 5)                  # Mon..Fri
            self.assertTrue(wk["label"].startswith("WEEK OF "))
        all_events = [e for wk in cal["weeks"] for d in wk["days"] for e in d["events"]]
        self.assertEqual(len(all_events), 1)                      # the CPI; no earnings leak
        cpi = all_events[0]
        self.assertEqual(cpi["time"], "8:30 am")
        self.assertEqual(cpi["period"], "May")
        # Every weekday with no event renders empty (frontend → "None scheduled").
        empties = [d for wk in cal["weeks"] for d in wk["days"] if not d["events"]]
        self.assertTrue(empties)
        self.assertTrue(all(d["label"].isupper() for wk in cal["weeks"] for d in wk["days"]))

    def test_earnings_track_parses_extra(self):
        cal = calendar.build_week_calendar(track="earnings", weeks=2)
        all_events = [e for wk in cal["weeks"] for d in wk["days"] for e in d["events"]]
        self.assertEqual(len(all_events), 1)                      # the AAPL earnings only
        ev = all_events[0]
        self.assertEqual(ev["symbols"], "AAPL")
        self.assertEqual(ev["period"], "Q3 2026")
        self.assertEqual(ev["extra"]["eps_est"], 1.5)


class RatesTest(unittest.TestCase):
    def test_no_key_returns_cta(self):
        orig = sources.fred_key
        sources.fred_key = lambda: ""
        try:
            r = calendar.build_rates()
            self.assertFalse(r["fred_keyed"])
            self.assertEqual(r["rates"], [])
            self.assertIn("FRED_API_KEY", r["note"])
        finally:
            sources.fred_key = orig

    def test_rates_deltas_and_inversion(self):
        fake = {
            "DGS10":  {"2026-06-15": 4.20, "2026-06-12": 4.15, "2026-06-08": 4.10},
            "DGS2":   {"2026-06-15": 4.55, "2026-06-12": 4.50},
            "T10Y2Y": {"2026-06-15": -0.35, "2026-06-12": -0.30},
            "T10Y3M": {"2026-06-15": 0.10},
        }
        ok, oo = sources.fred_key, sources.fred_observations
        sources.fred_key = lambda: "x"
        sources.fred_observations = lambda s, u, limit=24: fake.get(s, {})
        try:
            r = calendar.build_rates()
            self.assertTrue(r["fred_keyed"])
            by = {row["series"]: row for row in r["rates"]}
            self.assertEqual(by["DGS10"]["value"], 4.20)
            self.assertEqual(by["DGS10"]["d1"], 5)        # (4.20-4.15)*100 = 5 bps
            self.assertEqual(by["DGS2"]["d1"], 5)
            self.assertIsNone(by["DGS10"]["d21"])         # not enough history
            self.assertTrue(by["T10Y2Y"]["inverted"])     # negative spread
            self.assertTrue(r["inversion"]["inverted"])   # 10Y-2Y < 0
            self.assertEqual({c["tenor"] for c in r["curve"]}, {"2Y", "10Y"})  # only seeded tenors
        finally:
            sources.fred_key, sources.fred_observations = ok, oo


class NewsFeedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_dir, self._orig_db = store._DATA_DIR, store.DB_PATH
        self._orig_sources, self._orig_refresh = sources.ALL_SOURCES, calendar._last_refresh
        store._DATA_DIR = Path(self._tmp.name)
        store.DB_PATH   = Path(self._tmp.name) / "news.db"
        store.init_db()

        def news(date_off, t, title, extra):
            return dict(source=extra.get("source_name", "rss_news"), kind="news",
                        event_date=_d(date_off), event_time=t, title=title,
                        importance="low", detail=None,
                        symbols=",".join(extra.get("tickers", [])) or None,
                        url="http://x", extra=json.dumps(extra))

        sources.ALL_SOURCES = [FakeSource([
            news(0, "09:30", "Headline A", {"source_name": "CNBC", "tickers": ["AAPL"],
                                            "sentiment": "Bullish"}),
            news(0, "08:00", "Headline B", {"source_name": "MarketWatch"}),
            news(-1, None,   "MSFT: 8-K filed", {"source_name": "SEC EDGAR",
                                                 "tickers": ["MSFT"]}),
        ])]
        calendar._last_refresh = None

    def tearDown(self):
        store._DATA_DIR, store.DB_PATH = self._orig_dir, self._orig_db
        sources.ALL_SOURCES = self._orig_sources
        calendar._last_refresh = self._orig_refresh
        self._tmp.cleanup()

    def test_feed_groups_and_sorts(self):
        feed = calendar.build_news_feed(days_back=5)
        self.assertEqual(feed["count"], 3)
        self.assertEqual(len(feed["days"]), 2)            # today + yesterday
        self.assertTrue(feed["days"][0]["is_today"])      # most-recent day first
        today_items = feed["days"][0]["items"]
        self.assertEqual([i["title"] for i in today_items], ["Headline A", "Headline B"])  # time desc
        a = today_items[0]
        self.assertEqual(a["source"], "CNBC")
        self.assertEqual(a["tickers"], ["AAPL"])
        self.assertEqual(a["sentiment"], "Bullish")

    def test_news_kind_isolated_from_calendars(self):
        # News items must not leak into the econ/earnings week tracks.
        econ = calendar.build_week_calendar(track="econ", weeks=2)
        self.assertFalse([e for wk in econ["weeks"] for d in wk["days"] for e in d["events"]])


if __name__ == "__main__":
    unittest.main()
