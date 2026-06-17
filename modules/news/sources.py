"""
Swappable event sources for the news / macro-events module.

EventSource is the interface the calendar assembly sees; each concrete source
normalizes one feed into the common event dict and is **fail-soft** — any
network/parse error degrades to an empty list so the page never blocks.

v1 (calendar + Fed events) ships four sources, none of which require a paid key:
  FOMCSource         — the 8-meeting/yr Fed schedule (static table; no key)
  EconCalendarSource — econ releases via FRED if FRED_API_KEY is set, else an
                       approximate monthly generator (no key, clearly caveated)
  FedRSSSource       — Fed press releases + speeches RSS (no key)
  EarningsSource     — focus-list earnings dates reused from the screener store

Adding Finnhub/EDGAR/Marketaux later = add a class here; nothing in the
calendar assembly changes (the breadth/datasource.py adapter pattern).
"""

import calendar as _cal
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_UA   = "Mozilla/5.0 (MarketIntelHarness/1.0; +local)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_env():
    """Parse repo-root .env into a dict (keys live there, not always exported)."""
    env, path = {}, _ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fred_key():
    import os
    return os.environ.get("FRED_API_KEY") or _read_env().get("FRED_API_KEY") or ""


def finnhub_key():
    import os
    return os.environ.get("FINNHUB_API_KEY") or _read_env().get("FINNHUB_API_KEY") or ""


def _http_get(url, params=None, timeout=15, headers=None):
    """GET → raw text, or None on any failure (fail-soft)."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def alphavantage_key():
    import os
    return os.environ.get("ALPHAVANTAGE_API_KEY") or _read_env().get("ALPHAVANTAGE_API_KEY") or ""


def polygon_key():
    """Polygon.io / Massive key. NOTE the project-canonical name is POLYGON_IO_KEY
    (the flow stub reads the same)."""
    import os
    return os.environ.get("POLYGON_IO_KEY") or _read_env().get("POLYGON_IO_KEY") or ""


def _parse_dt(raw):
    """RSS/Atom date string → datetime (tz-aware when given), or None."""
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except Exception:
            continue
    return None


def _parse_rss(raw):
    """→ [{title, link, dt}] from an RSS <item> feed (fail-soft → [])."""
    out = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip() or None
        pub  = (item.findtext("pubDate")
                or item.findtext("{http://purl.org/dc/elements/1.1/}date"))
        out.append({"title": title, "link": link, "dt": _parse_dt(pub)})
    return out


def _event(source, kind, event_date, title, detail=None, importance="low",
           symbols=None, url=None, event_time=None, extra=None):
    if isinstance(event_date, (date, datetime)):
        event_date = event_date.strftime("%Y-%m-%d")
    if symbols and not isinstance(symbols, str):
        symbols = ",".join(symbols)
    if extra is not None and not isinstance(extra, str):
        extra = json.dumps(extra)
    return {
        "source": source, "kind": kind, "event_date": event_date,
        "event_time": event_time, "title": title, "detail": detail,
        "importance": importance, "symbols": symbols, "url": url, "extra": extra,
    }


# Keyword → importance for econ releases / headlines. First match wins.
_HIGH_KW = ("employment situation", "nonfarm", "consumer price", "cpi",
            "gross domestic product", "personal income and outlays", "pce",
            "fomc", "federal funds", "pce price")
_MED_KW  = ("producer price", "ppi", "retail sales", "ism", "jobless",
            "initial claims", "unemployment insurance", "housing starts",
            "durable goods", "consumer sentiment", "consumer confidence",
            "job openings", "jolts", "industrial production")


def classify_importance(name):
    low = (name or "").lower()
    if any(k in low for k in _HIGH_KW):
        return "high"
    if any(k in low for k in _MED_KW):
        return "med"
    return "low"


def _first_weekday(year, month, weekday):
    """First `weekday` (Mon=0…Sun=6) of the month."""
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def _last_business_day(year, month):
    d = date(year, month, _cal.monthrange(year, month)[1])
    while d.weekday() >= 5:               # back off Sat/Sun
        d -= timedelta(days=1)
    return d


def _months_in_window(start, end):
    """Yield (year, month) for every month touched by [start, end]."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class EventSource:
    name = "base"

    def fetch(self, start, end):
        """→ list[event dict] with start <= event_date <= end (date objects)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# FOMC schedule (static table — no key, reliable)
# ---------------------------------------------------------------------------

class FOMCSource(EventSource):
    name = "fomc"

    # Decision dates (the second meeting day = statement + press conference).
    # SEP meetings (Mar/Jun/Sep/Dec) also publish the dot-plot projections.
    # Static for reliability — verify/refresh annually from
    # federalreserve.gov/monetarypolicy/fomccalendars.htm.
    MEETINGS = [
        ("2025-01-29", False), ("2025-03-19", True),  ("2025-05-07", False),
        ("2025-06-18", True),  ("2025-07-30", False), ("2025-09-17", True),
        ("2025-10-29", False), ("2025-12-10", True),
        ("2026-01-28", False), ("2026-03-18", True),  ("2026-04-29", False),
        ("2026-06-17", True),  ("2026-07-29", False), ("2026-09-16", True),
        ("2026-10-28", False), ("2026-12-09", True),
    ]

    def fetch(self, start, end):
        out = []
        for d_str, has_sep in self.MEETINGS:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
            url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
            if start <= d <= end:
                detail = "Rate decision · statement + press conference (2:00 PM ET)"
                if has_sep:
                    detail += " · Summary of Economic Projections (dot plot)"
                out.append(_event("fomc", "fomc", d, "FOMC rate decision",
                                  detail=detail, importance="high",
                                  event_time="14:00", url=url))
            # Minutes are released ~3 weeks after the meeting.
            minutes = d + timedelta(days=21)
            if start <= minutes <= end:
                out.append(_event("fomc", "fed_news", minutes,
                                  "FOMC minutes released",
                                  detail=f"Minutes of the {d_str} meeting (2:00 PM ET)",
                                  importance="med", event_time="14:00", url=url))
        return out


# ---------------------------------------------------------------------------
# Economic calendar — FRED if keyed, else an approximate monthly generator
# ---------------------------------------------------------------------------

class EconCalendarSource(EventSource):
    name = "econ"

    FRED_DATES = "https://api.stlouisfed.org/fred/releases/dates"

    def fetch(self, start, end):
        key = fred_key()
        if key:
            events = self._fetch_fred(key, start, end)
            if events is not None:
                return events
        return self._approx(start, end)

    # -- FRED (exact) -------------------------------------------------------

    def _fetch_fred(self, key, start, end):
        raw = _http_get(self.FRED_DATES, {
            "api_key": key, "file_type": "json",
            "include_release_dates_with_no_data": "true",
            "sort_order": "asc", "limit": 1000,
            "realtime_start": start.strftime("%Y-%m-%d"),
        })
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        out = []
        for r in payload.get("release_dates", []):
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            except Exception:
                continue
            if not (start <= d <= end):
                continue
            name = r.get("release_name", "Economic release")
            imp  = classify_importance(name)
            if imp == "low":            # keep the calendar to the market-movers
                continue
            out.append(_event("fred", "econ", d, name,
                              detail="Scheduled data release (FRED)",
                              importance=imp,
                              url="https://www.stlouisfed.org/economy"))
        return out

    # -- Approximate (no key) ----------------------------------------------

    def _approx(self, start, end):
        """Predictable-cadence monthly releases when no FRED key is set. Dates
        are approximate (NFP is exact — first Friday; the rest are nominal) and
        flagged as such; add FRED_API_KEY for the exact published calendar."""
        note = "Approximate — set FRED_API_KEY for exact release dates"
        out  = []
        for y, m in _months_in_window(start, end):
            nfp = _first_weekday(y, m, 4)                       # first Friday
            specs = [
                (nfp,                  "Employment Situation (Nonfarm Payrolls)", "high", "08:30"),
                (date(y, m, 12),       "Consumer Price Index (CPI)",             "high", "08:30"),
                (date(y, m, 13),       "Producer Price Index (PPI)",             "med",  "08:30"),
                (date(y, m, 16),       "Advance Retail Sales",                   "med",  "08:30"),
                (_last_business_day(y, m), "Personal Income & Outlays (PCE)",    "high", "08:30"),
            ]
            if m in (1, 4, 7, 10):                              # quarterly GDP
                specs.append((date(y, m, 28), "Gross Domestic Product (GDP)", "high", "08:30"))
            for d, title, imp, t in specs:
                if start <= d <= end:
                    out.append(_event("econ_approx", "econ", d, title,
                                      detail=note, importance=imp, event_time=t,
                                      url="https://www.bls.gov/schedule/news_release/"))
        return out


# ---------------------------------------------------------------------------
# FRED observations → econ Actual / Previous (free with FRED_API_KEY; revised)
# ---------------------------------------------------------------------------

FRED_OBS   = "https://api.stlouisfed.org/fred/series/observations"
_OBS_CACHE = {}            # (series, units) → (monotonic_ts, {date: float})
_OBS_TTL   = 1800

# report-title keyword(s) → (FRED series, FRED `units` transform, display unit, quarterly?)
# units: pch = period-over-period % (MoM for monthly); chg = level change; lin = level.
# Curated to releases whose period aligns to "reports the prior month" — weekly series
# (jobless claims) are deliberately excluded so we never show a misaligned number.
ECON_SERIES = [
    (("core cpi",),                                  "CPILFESL",        "pch", "pct",      False),
    (("consumer price index", "cpi"),                "CPIAUCSL",        "pch", "pct",      False),
    (("core pce",),                                  "PCEPILFE",        "pch", "pct",      False),
    (("personal income", "pce"),                     "PCEPI",           "pch", "pct",      False),
    (("producer price index", "ppi"),                "PPIFIS",          "pch", "pct",      False),
    (("retail",),                                    "RSAFS",           "pch", "pct",      False),
    (("employment situation", "nonfarm", "payroll"), "PAYEMS",          "chg", "payrolls", False),
    (("unemployment rate",),                         "UNRATE",          "lin", "pct",      False),
    (("gross domestic product", "gdp"),              "A191RL1Q225SBEA", "lin", "pct",      True),
]


def match_series(title):
    low = (title or "").lower()
    for keys, series, units, unit, is_q in ECON_SERIES:
        if any(k in low for k in keys):
            return (series, units, unit, is_q)
    return None


def fred_observations(series, units, limit=24):
    """{observation_date: value} for ~2y, FRED-transformed by `units`. Cached per
    (series, units, limit) for the TTL. Fail-soft → {} (incl. no key)."""
    ck  = (series, units, limit)
    hit = _OBS_CACHE.get(ck)
    if hit and (time.monotonic() - hit[0]) < _OBS_TTL:
        return hit[1]
    key = fred_key()
    if not key:
        return {}
    raw = _http_get(FRED_OBS, {
        "series_id": series, "api_key": key, "file_type": "json",
        "units": units, "sort_order": "desc", "limit": limit,
        "observation_start": (date.today() - timedelta(days=760)).strftime("%Y-%m-%d"),
    })
    out = {}
    if raw:
        try:
            for o in json.loads(raw).get("observations", []):
                v = o.get("value")
                if v not in (None, "", "."):
                    out[o["date"]] = float(v)
        except Exception:
            out = {}
    _OBS_CACHE[ck] = (time.monotonic(), out)
    return out


def format_econ_value(v, unit):
    if v is None:
        return ""
    try:
        if unit == "pct":
            return f"{v:.1f}%"
        if unit == "payrolls":              # PAYEMS chg is in thousands of persons
            return f"{v * 1000:+,.0f}"
        if unit == "count":
            return f"{v:,.0f}"
    except Exception:
        return ""
    return str(v)


def _quarter_start(d):
    return date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)


def econ_actual_previous(title, event_date):
    """(actual, previous) display strings from FRED observations. A release in month M
    reports M-1, so Actual is blank until that period is published (upcoming releases show
    only Previous). Values are FRED's latest (revised) numbers — consensus isn't free."""
    m = match_series(title)
    if not m:
        return ("", "")
    series, units, unit, is_q = m
    obs = fred_observations(series, units)
    if not obs:
        return ("", "")
    try:
        first = datetime.strptime(event_date, "%Y-%m-%d").date().replace(day=1)
    except Exception:
        return ("", "")
    if is_q:
        ref  = _quarter_start(first - timedelta(days=1))
        prev = _quarter_start(ref - timedelta(days=1))
    else:
        ref  = (first - timedelta(days=1)).replace(day=1)
        prev = (ref - timedelta(days=1)).replace(day=1)
    return (format_econ_value(obs.get(ref.strftime("%Y-%m-%d")), unit),
            format_econ_value(obs.get(prev.strftime("%Y-%m-%d")), unit))


# ---------------------------------------------------------------------------
# Fed RSS — press releases + speeches (no key, recent/backward-looking)
# ---------------------------------------------------------------------------

class FedRSSSource(EventSource):
    name = "fed_rss"

    FEEDS = [
        ("https://www.federalreserve.gov/feeds/press_monetary.xml", "fed_news",   "Fed press release"),
        ("https://www.federalreserve.gov/feeds/speeches.xml",       "fed_speech", "Fed speech"),
    ]

    def fetch(self, start, end):
        out = []
        for url, kind, label in self.FEEDS:
            out += self._parse_feed(url, kind, label, start, end)
        return out

    def _parse_feed(self, url, kind, label, start, end):
        raw = _http_get(url)
        if not raw:
            return []
        try:
            root = ET.fromstring(raw)
        except Exception:
            return []
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            link    = (item.findtext("link") or "").strip() or None
            pub_raw = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date")
            d = self._parse_date(pub_raw)
            if d is None or not (start <= d <= end):
                continue
            imp = classify_importance(title)
            out.append(_event("fed_rss", kind, d, f"{label}: {title}",
                              importance=("med" if imp == "low" else imp), url=link))
        return out

    @staticmethod
    def _parse_date(raw):
        if not raw:
            return None
        try:
            return parsedate_to_datetime(raw).date()
        except Exception:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw.strip(), fmt).date()
            except Exception:
                continue
        return None


# ---------------------------------------------------------------------------
# Earnings calendar — Finnhub (broad + EPS) when keyed, else screener focus list
# ---------------------------------------------------------------------------

class EarningsCalendarSource(EventSource):
    name = "earnings"

    FINNHUB = "https://finnhub.io/api/v1/calendar/earnings"
    CAP     = 250                # keyless flood guard if no relevance set exists

    def fetch(self, start, end):
        key = finnhub_key()
        if key:
            evs = self._fetch_finnhub(key, start, end)
            if evs:                      # fall through to focus list if empty/failed
                return evs
        return self._fetch_focus(start, end)

    # -- Finnhub (broad coverage + EPS estimate/actual) --------------------

    def _fetch_finnhub(self, key, start, end):
        raw = _http_get(self.FINNHUB, {
            "from": start.strftime("%Y-%m-%d"),
            "to":   end.strftime("%Y-%m-%d"),
            "token": key,
        })
        if not raw:
            return []
        try:
            rows = json.loads(raw).get("earningsCalendar", []) or []
        except Exception:
            return []
        relevant = self._relevant_symbols()      # keep the calendar to names that matter
        out = []
        for r in rows:
            sym = (r.get("symbol") or "").strip()
            if not sym or (relevant and sym not in relevant):
                continue
            try:
                d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            except Exception:
                continue
            if not (start <= d <= end):
                continue
            out.append(self._earn_event(sym, d, hour=r.get("hour"),
                                        eps_est=r.get("epsEstimate"),
                                        eps_actual=r.get("epsActual"),
                                        quarter=r.get("quarter"), year=r.get("year")))
            if not relevant and len(out) >= self.CAP:
                break
        return out

    @staticmethod
    def _relevant_symbols():
        """Focus list ∪ S&P 500 members — best-effort, fail-soft (empty ⇒ no filter)."""
        syms = set()
        try:
            from modules.screener.poller import focus_list
            syms |= set(focus_list())
        except Exception:
            pass
        try:
            from modules.breadth import store as breadth_store
            syms |= set(breadth_store.get_members("sp500"))
        except Exception:
            pass
        return syms

    # -- Focus-list fallback (no key; date + symbol only) ------------------

    def _fetch_focus(self, start, end):
        try:
            from modules.screener import store as screener_store
            df = screener_store.get_fundamentals()
        except Exception:
            return []
        if df is None or df.empty or "earnings_date" not in df.columns:
            return []
        out = []
        for symbol, row in df.iterrows():
            ed = row.get("earnings_date")
            if not ed:
                continue
            try:
                d = datetime.strptime(str(ed)[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if start <= d <= end:
                out.append(self._earn_event(symbol, d))
        return out

    @staticmethod
    def _earn_event(symbol, d, hour=None, eps_est=None, eps_actual=None,
                    quarter=None, year=None):
        period = (f"Q{quarter} {year}" if quarter and year else None)
        extra  = {"when": hour, "eps_est": eps_est, "eps_actual": eps_actual,
                  "period": period}
        return _event("earnings", "earnings", d, symbol,
                      detail="Scheduled earnings report", importance="med",
                      symbols=[symbol], extra=extra)


# ---------------------------------------------------------------------------
# Phase 2 — news feed (kind="news"): market RSS + SEC 8-K + AlphaVantage sentiment
# ---------------------------------------------------------------------------

_SEC_UA = "MarketIntelHarness/1.0 (personal research; contact via local app)"


class MarketNewsRSSSource(EventSource):
    """General market headlines from free RSS feeds (no key)."""
    name = "market_news"

    FEEDS = [
        ("CNBC",           "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("CNBC Markets",   "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
        ("MarketWatch",    "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("Yahoo Finance",  "https://finance.yahoo.com/news/rssindex"),
    ]

    def fetch(self, start, end):
        out = []
        for label, url in self.FEEDS:
            raw = _http_get(url)
            if not raw:
                continue
            for it in _parse_rss(raw):
                dt = it["dt"]
                if dt is None or not (start <= dt.date() <= end):
                    continue
                out.append(_event("rss_news", "news", dt.date(), it["title"],
                                  importance="low", url=it["link"],
                                  event_time=dt.strftime("%H:%M"),
                                  extra={"source_name": label}))
        return out


class Edgar8KSource(EventSource):
    """SEC 8-K material events for the focus list (no key). ticker→CIK via SEC's
    company_tickers.json (cached); one most-recent 8-K per symbol within the window."""
    name = "edgar"

    SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    _cik        = {"ts": 0.0, "map": {}}

    def fetch(self, start, end):
        focus = self._focus()
        if not focus:
            return []
        cikmap = self._cik_map()
        if not cikmap:
            return []
        out = []
        for sym in list(focus)[:25]:
            cik = cikmap.get(sym.upper())
            if not cik:
                continue
            raw = _http_get(self.SUBMISSIONS.format(cik=cik), timeout=20,
                            headers={"User-Agent": _SEC_UA})
            if not raw:
                continue
            try:
                recent = json.loads(raw)["filings"]["recent"]
                forms, dates = recent["form"], recent["filingDate"]
            except Exception:
                continue
            for i, form in enumerate(forms):       # arrays are newest-first
                if form != "8-K":
                    continue
                try:
                    d = datetime.strptime(dates[i], "%Y-%m-%d").date()
                except Exception:
                    continue
                if start <= d <= end:
                    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K"
                    out.append(_event("edgar", "news", d, f"{sym}: 8-K filed",
                                      detail="SEC material event (8-K)", importance="med",
                                      symbols=[sym], url=url,
                                      extra={"source_name": "SEC EDGAR", "tickers": [sym]}))
                break                              # one (latest) per symbol is enough
        return out

    @staticmethod
    def _focus():
        try:
            from modules.screener.poller import focus_list
            return set(focus_list())
        except Exception:
            return set()

    @classmethod
    def _cik_map(cls):
        if cls._cik["map"] and (time.monotonic() - cls._cik["ts"]) < 86400:
            return cls._cik["map"]
        raw = _http_get(cls.TICKERS_URL, timeout=20, headers={"User-Agent": _SEC_UA})
        m = {}
        if raw:
            try:
                for row in json.loads(raw).values():
                    m[str(row["ticker"]).upper()] = int(row["cik_str"])
            except Exception:
                m = {}
        if m:
            cls._cik = {"ts": time.monotonic(), "map": m}
        return m or cls._cik["map"]


class AlphaVantageNewsSource(EventSource):
    """News + sentiment + ticker tagging (keyed, fail-soft). Free tier ~25 req/day."""
    name = "alphavantage"

    AV = "https://www.alphavantage.co/query"

    def fetch(self, start, end):
        key = alphavantage_key()
        if not key:
            return []
        raw = _http_get(self.AV, {"function": "NEWS_SENTIMENT", "apikey": key,
                                  "sort": "LATEST", "limit": 50,
                                  "topics": "financial_markets"})
        if not raw:
            return []
        try:
            feed = json.loads(raw).get("feed", []) or []
        except Exception:
            return []
        out = []
        for a in feed:
            try:
                dt = datetime.strptime(a.get("time_published", ""), "%Y%m%dT%H%M%S")
            except Exception:
                continue
            if not (start <= dt.date() <= end):
                continue
            tickers = [t.get("ticker") for t in (a.get("ticker_sentiment") or [])
                       if t.get("ticker")][:6]
            out.append(_event("alphavantage", "news", dt.date(),
                              a.get("title") or "(untitled)", importance="low",
                              url=a.get("url"), event_time=dt.strftime("%H:%M"),
                              symbols=tickers or None,
                              extra={"source_name": a.get("source") or "AlphaVantage",
                                     "sentiment": a.get("overall_sentiment_label"),
                                     "tickers": tickers}))
        return out


_POLY_SENTIMENT = {"positive": "Bullish", "negative": "Bearish", "neutral": "Neutral"}


class PolygonNewsSource(EventSource):
    """Ticker news + Benzinga-derived sentiment & insights (keyed: POLYGON_IO_KEY).

    Polygon/Massive's FREE tier grants this news endpoint — the OPRA options tape and
    chain snapshots it would feed the flow module do NOT (they 403 NOT_AUTHORIZED on
    free; a paid Options plan is required). So the free-tier value lands here: richer
    than the bare RSS feeds (per-article ticker tags + a sentiment label) and a far
    more generous rate limit than AlphaVantage's ~25/day. Fail-soft → []."""
    name = "polygon_news"

    URL = "https://api.polygon.io/v2/reference/news"

    def fetch(self, start, end):
        key = polygon_key()
        if not key:
            return []
        raw = _http_get(self.URL, {"limit": 100, "order": "desc",
                                   "sort": "published_utc", "apiKey": key})
        if not raw:
            return []
        try:
            results = json.loads(raw).get("results", []) or []
        except Exception:
            return []
        out, seen = [], set()
        for a in results:
            dt = _parse_dt(a.get("published_utc"))
            if dt is None or not (start <= dt.date() <= end):
                continue
            title = (a.get("title") or "").strip()
            if not title:
                continue
            tickers   = [t for t in (a.get("tickers") or []) if t][:6]
            publisher = (a.get("publisher") or {}).get("name") or "Polygon"
            # Polygon reposts wire press releases in many languages (title, URL AND
            # description are all translated) — the stable identity is the same wire at
            # the same instant on the same tickers. Fall back to the title for tickerless
            # items so distinct general-market headlines aren't over-collapsed.
            kdup = ((a.get("published_utc"), publisher, tuple(sorted(tickers)))
                    if tickers else (a.get("published_utc"), publisher, title))
            if kdup in seen:
                continue
            seen.add(kdup)
            out.append(_event("polygon_news", "news", dt.date(), title,
                              importance="low", url=a.get("article_url"),
                              event_time=dt.strftime("%H:%M"),
                              symbols=tickers or None,
                              extra={"source_name": publisher,
                                     "sentiment": self._sentiment(a.get("insights")),
                                     "tickers": tickers}))
        return out

    @staticmethod
    def _sentiment(insights):
        """Polygon's per-ticker insights → one overall label the feed's pill renders
        (Bullish/Bearish/Neutral). Majority vote, with neutral broken only if it's the
        sole read so a tagged direction wins. None when there are no insights."""
        if not insights:
            return None
        votes = {}
        for ins in insights:
            s = (ins.get("sentiment") or "").lower()
            if s in _POLY_SENTIMENT:
                votes[s] = votes.get(s, 0) + 1
        if not votes:
            return None
        directional = {k: v for k, v in votes.items() if k != "neutral"}
        tally = directional or votes
        return _POLY_SENTIMENT[max(tally, key=tally.get)]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ALL_SOURCES = [
    FOMCSource(), EconCalendarSource(), FedRSSSource(), EarningsCalendarSource(),
    MarketNewsRSSSource(), Edgar8KSource(), AlphaVantageNewsSource(),
    PolygonNewsSource(),
]
