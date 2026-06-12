"""
Swappable market-data adapters for the breadth module.

DataSource is the interface indicator code sees; SchwabDataSource (default)
and YFinanceDataSource implement it. Adding Polygon/EODHD later means adding
a class here — nothing in indicators/regime/backfill changes.

Schwab constraints designed around: 120 requests/minute hard limit, price
history is one symbol per request (quotes can be batched).
"""

import threading
import time
from datetime import datetime, timedelta

import pandas as pd

try:
    import requests
except ImportError:
    requests = None

try:
    import yfinance as yf
except ImportError:
    yf = None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Paces calls to a fixed per-minute budget (thread-safe)."""

    def __init__(self, per_minute):
        self._interval = 60.0 / per_minute
        self._lock     = threading.Lock()
        self._next_ok  = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next_ok:
                delay = self._next_ok - now
                self._next_ok += self._interval
            else:
                delay = 0.0
                self._next_ok = now + self._interval
        if delay > 0:
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class DataSource:
    """Daily OHLCV provider. Symbols use canonical dot form (BRK.B);
    adapters translate to their own conventions."""

    name          = "base"
    supports_bulk = False

    def get_price_history(self, symbol, start, end):
        """→ DataFrame columns: date (YYYY-MM-DD str), open, high, low, close, volume."""
        raise NotImplementedError

    def get_quotes(self, symbols):
        """→ {symbol: last_price}"""
        raise NotImplementedError

    def bulk_price_history(self, symbols, start, end):
        """Optional: → {symbol: DataFrame}. Only when supports_bulk."""
        raise NotImplementedError


def _empty_frame():
    return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])


# ---------------------------------------------------------------------------
# Schwab
# ---------------------------------------------------------------------------

class SchwabDataSource(DataSource):
    name          = "schwab"
    supports_bulk = False
    MARKET_DATA   = "https://api.schwabapi.com/marketdata/v1"

    def __init__(self, per_minute=110):
        # 110/min leaves margin under the documented 120/min hard limit.
        self._limiter = RateLimiter(per_minute)

    @staticmethod
    def _translate(symbol):
        if symbol.startswith("$") or symbol.startswith("^"):
            return symbol            # index symbols pass through
        return symbol.replace(".", "/")   # BRK.B → BRK/B

    def _headers(self):
        # Imported lazily: schwab module owns OAuth + token refresh.
        from modules.schwab import get_access_token
        return {"Authorization": f"Bearer {get_access_token()}"}

    def _get(self, url, params):
        last_err = None
        for attempt in range(4):
            self._limiter.wait()
            try:
                r = requests.get(url, params=params, headers=self._headers(), timeout=20)
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_err = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(max(2 ** attempt, float(r.headers.get("Retry-After", 0) or 0)))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Schwab request failed after retries: {last_err}")

    def get_price_history(self, symbol, start, end):
        start_ms = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
        # +1 day so the end date itself is included.
        end_ms   = int((datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).timestamp() * 1000)
        data = self._get(f"{self.MARKET_DATA}/pricehistory", {
            "symbol":        self._translate(symbol),
            "periodType":    "year",
            "frequencyType": "daily",
            "frequency":     1,
            "startDate":     start_ms,
            "endDate":       end_ms,
        })
        candles = data.get("candles") or []
        if not candles:
            return _empty_frame()
        df = pd.DataFrame(candles)
        ts = pd.to_datetime(df["datetime"], unit="ms", utc=True).dt.tz_convert("America/New_York")
        df["date"] = ts.dt.strftime("%Y-%m-%d")
        return df[["date", "open", "high", "low", "close", "volume"]]

    def get_quotes(self, symbols):
        out = {}
        for i in range(0, len(symbols), 300):
            chunk = [self._translate(s) for s in symbols[i:i + 300]]
            data  = self._get(f"{self.MARKET_DATA}/quotes", {"symbols": ",".join(chunk)})
            for sym, q in data.items():
                px = (q.get("quote") or {}).get("lastPrice")
                if px is not None:
                    out[sym.replace("/", ".")] = px
        return out


# ---------------------------------------------------------------------------
# yfinance
# ---------------------------------------------------------------------------

class YFinanceDataSource(DataSource):
    name          = "yfinance"
    supports_bulk = True
    CHUNK         = 200

    @staticmethod
    def _translate(symbol):
        if symbol.startswith("^"):
            return symbol
        return symbol.replace(".", "-")   # BRK.B → BRK-B

    @staticmethod
    def _normalize(raw):
        if raw is None or raw.empty:
            return _empty_frame()
        df = raw.reset_index()
        df.columns = [str(c).lower() for c in df.columns]
        date_col = "date" if "date" in df.columns else df.columns[0]
        out = pd.DataFrame({
            "date":   pd.to_datetime(df[date_col]).dt.strftime("%Y-%m-%d"),
            "open":   df["open"], "high": df["high"], "low": df["low"],
            "close":  df["close"], "volume": df["volume"],
        })
        return out.dropna(subset=["close"])

    def get_price_history(self, symbol, start, end):
        raw = yf.download(self._translate(symbol), start=start,
                          end=(datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"),
                          auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        return self._normalize(raw)

    def bulk_price_history(self, symbols, start, end):
        end_excl = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        out = {}
        for i in range(0, len(symbols), self.CHUNK):
            chunk = symbols[i:i + self.CHUNK]
            yf_map = {self._translate(s): s for s in chunk}
            raw = yf.download(list(yf_map), start=start, end=end_excl,
                              auto_adjust=True, progress=False,
                              group_by="ticker", threads=True)
            if raw is None or raw.empty:
                continue
            if not isinstance(raw.columns, pd.MultiIndex):
                # single ticker came back flat
                out[chunk[0]] = self._normalize(raw)
                continue
            for yf_sym, canonical in yf_map.items():
                if yf_sym in raw.columns.get_level_values(0):
                    out[canonical] = self._normalize(raw[yf_sym].dropna(how="all"))
        return out

    def get_quotes(self, symbols):
        out = {}
        bulk = self.bulk_price_history(
            symbols,
            (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
            datetime.now().strftime("%Y-%m-%d"),
        )
        for sym, df in bulk.items():
            if not df.empty:
                out[sym] = float(df["close"].iloc[-1])
        return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_SOURCES = {"schwab": SchwabDataSource, "yfinance": YFinanceDataSource}


def get_datasource(name):
    if name not in _SOURCES:
        raise ValueError(f"Unknown datasource '{name}' (have: {sorted(_SOURCES)})")
    return _SOURCES[name]()


def resolve_datasource(preferred="schwab"):
    """Return (source, note). Falls back to yfinance when Schwab can't
    produce a token or lacks market-data entitlement."""
    if preferred == "schwab":
        try:
            src = SchwabDataSource()
            src._headers()   # raises if never authenticated
            return src, None
        except Exception as e:
            return YFinanceDataSource(), f"Schwab unavailable ({e}) — using yfinance"
    return get_datasource(preferred), None
