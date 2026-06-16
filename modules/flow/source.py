"""
Pluggable options-data source adapters.

`OptionsFlowSource` is the interface the poller/scoring see. `SchwabOptionsSource`
(default) serves chain SNAPSHOTS — per-contract volume, open interest, bid/ask/last/
mark, IV, greeks — which is everything the VOL/OI + size + timeframe + next-day-OI
rules need. What Schwab snapshots CANNOT give is the OPRA trade tape, so the trader's
two tape-only signals are degraded, honestly:
  * Rule 1 A/AA aggressor → ESTIMATED from last-vs-bid/ask (see scoring), never confirmed.
  * Rule 4 sweeps/blocks → unavailable (the poller still detects clusters from diffs).

`PolygonOptionsSource` is a pluggable stub: when its `get_trades()` is implemented it
flips `supports_aggressor`/`supports_trade_tape` to True and the scoring engine reads
confirmed A/AA + sweep/block tags with NO change to the scoring code. Adding a source =
add a class here, exactly like `breadth/datasource.py`.
"""

import time
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    requests = None

from modules.breadth.datasource import RateLimiter   # reuse the shared pacer


def _num(v):
    """Schwab reports missing greeks/IV as -999.0 or 'NaN' — coerce to float|None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= -998.0 or f != f:      # sentinel / NaN
        return None
    return f


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

class OptionsFlowSource:
    name                = "base"
    supports_aggressor  = False    # confirmed A/AA from the trade tape
    supports_trade_tape = False    # individual prints → sweeps/blocks/splits

    def get_chain(self, symbol):
        """→ list[contract dict] — a near-the-money snapshot of one underlying."""
        raise NotImplementedError

    def get_trades(self, symbol, since=None):
        """→ list[trade dict] with confirmed side + sweep/block tags. Snapshot
        sources return []."""
        return []

    def capabilities(self):
        return {
            "source":    self.name,
            "tier":      "tape" if self.supports_trade_tape else "snapshot",
            "aggressor": "confirmed" if self.supports_aggressor else "estimated",
        }


# ---------------------------------------------------------------------------
# Schwab (default) — chain snapshots
# ---------------------------------------------------------------------------

class SchwabOptionsSource(OptionsFlowSource):
    name        = "schwab"
    MARKET_DATA = "https://api.schwabapi.com/marketdata/v1"

    def __init__(self, per_minute=110, strike_count=50, dte_max=400):
        self._limiter     = RateLimiter(per_minute)
        self._strike_count = strike_count    # nearest-to-ATM strikes (bounds payload + OTM reach)
        self._dte_max      = dte_max         # don't pull expiries beyond this

    def _headers(self):
        from modules.schwab import get_access_token   # schwab owns OAuth
        return {"Authorization": f"Bearer {get_access_token()}"}

    def _get(self, url, params):
        last_err = None
        for attempt in range(4):
            self._limiter.wait()
            try:
                r = requests.get(url, params=params, headers=self._headers(), timeout=20)
            except requests.RequestException as e:
                last_err = e; time.sleep(2 ** attempt); continue
            if r.status_code == 429 or r.status_code >= 500:
                last_err = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(max(2 ** attempt, float(r.headers.get("Retry-After", 0) or 0)))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Schwab chains request failed after retries: {last_err}")

    def get_chain(self, symbol):
        to_date = (datetime.now() + timedelta(days=self._dte_max)).strftime("%Y-%m-%d")
        data = self._get(f"{self.MARKET_DATA}/chains", {
            "symbol":                 symbol,
            "contractType":           "ALL",
            "strikeCount":            self._strike_count,
            "includeUnderlyingQuote": "true",
            "toDate":                 to_date,
        })
        if not data or data.get("status") == "FAILED":
            return []
        spot = _num(data.get("underlyingPrice"))
        if spot is None:
            spot = _num((data.get("underlying") or {}).get("last"))
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for key in ("callExpDateMap", "putExpDateMap"):
            for _exp, strikes in (data.get(key) or {}).items():
                for _strike, contracts in strikes.items():
                    for k in contracts:
                        out.append(self._parse_contract(symbol, k, spot, ts))
        return out

    @staticmethod
    def _parse_contract(underlying, k, spot, ts):
        exp = k.get("expirationDate")          # ms epoch or ISO; keep the date part
        if isinstance(exp, (int, float)):
            exp = datetime.utcfromtimestamp(exp / 1000).strftime("%Y-%m-%d")
        elif isinstance(exp, str):
            exp = exp[:10]
        return {
            "underlying":     underlying,
            "option_symbol":  k.get("symbol"),
            "put_call":       k.get("putCall"),
            "strike":         _num(k.get("strikePrice")),
            "expiry":         exp,
            "dte":            int(k.get("daysToExpiration")) if k.get("daysToExpiration") is not None else None,
            "bid":            _num(k.get("bid")),
            "ask":            _num(k.get("ask")),
            "last":           _num(k.get("last")),
            "mark":           _num(k.get("mark")),
            "session_volume": int(k.get("totalVolume") or 0),
            "open_interest":  int(k.get("openInterest") or 0),
            "iv":             _num(k.get("volatility")),
            "delta":          _num(k.get("delta")),
            "spot":           spot,
            "ts":             ts,
        }


# ---------------------------------------------------------------------------
# Polygon (pluggable stub) — trade tape with confirmed A/AA + sweeps/blocks
# ---------------------------------------------------------------------------

class PolygonOptionsSource(OptionsFlowSource):
    name                = "polygon"
    supports_aggressor  = True
    supports_trade_tape = True

    def __init__(self):
        from modules.schwab import _read_env
        self.key = _read_env().get("POLYGON_API_KEY")

    def get_chain(self, symbol):
        raise NotImplementedError("PolygonOptionsSource.get_chain not implemented yet")

    def get_trades(self, symbol, since=None):
        raise NotImplementedError("PolygonOptionsSource.get_trades not implemented yet")


_SOURCES = {"schwab": SchwabOptionsSource, "polygon": PolygonOptionsSource}


def resolve_source(preferred="schwab"):
    """Return (source, note). Polygon is selected only if its key is present and it's
    asked for; otherwise Schwab. Never raises — the poller is fail-soft on fetch."""
    if preferred == "polygon":
        try:
            src = PolygonOptionsSource()
            if src.key:
                return src, None
            return SchwabOptionsSource(), "POLYGON_API_KEY not set — using Schwab snapshots"
        except Exception as e:
            return SchwabOptionsSource(), f"Polygon unavailable ({e}) — using Schwab snapshots"
    return SchwabOptionsSource(), None
