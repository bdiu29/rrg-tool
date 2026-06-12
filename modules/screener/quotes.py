"""
Schwab market-data wrappers for the screener: rich intraday quotes (running
total volume — the field yfinance can't provide reliably) and batched
fundamentals from the instruments endpoint.

Field names verified live 2026-06-11: instruments?projection=fundamental
accepts comma-separated symbols (100/call confirmed) and returns marketCap,
sharesOutstanding, avg10DaysVolume, peRatio, dividendYield, beta.
"""

import time

import requests

from modules.breadth.datasource import RateLimiter

MARKET_DATA = "https://api.schwabapi.com/marketdata/v1"
QUOTE_BATCH = 300
FUND_BATCH  = 100

# Shared across quote + instrument calls; deliberately below breadth's 110/min
# so a poller tick and a running bar sync can coexist under Schwab's 120/min.
_limiter = RateLimiter(60)


def _translate(symbol):
    return symbol if symbol.startswith(("$", "^")) else symbol.replace(".", "/")


def _untranslate(symbol):
    return symbol.replace("/", ".")


def _headers():
    from modules.schwab import get_access_token
    return {"Authorization": f"Bearer {get_access_token()}"}


def _get(url, params):
    last_err = None
    for attempt in range(4):
        _limiter.wait()
        try:
            r = requests.get(url, params=params, headers=_headers(), timeout=20)
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


def get_rich_quotes(symbols):
    """→ {symbol: {last, total_volume, open, prev_close, net_pct_chg}}"""
    out = {}
    for i in range(0, len(symbols), QUOTE_BATCH):
        chunk = [_translate(s) for s in symbols[i:i + QUOTE_BATCH]]
        data  = _get(f"{MARKET_DATA}/quotes",
                     {"symbols": ",".join(chunk), "fields": "quote"})
        for sym, payload in data.items():
            if sym == "errors":
                continue
            q = payload.get("quote") or {}
            if q.get("lastPrice") is None:
                continue
            out[_untranslate(sym)] = {
                "last":         q.get("lastPrice"),
                "total_volume": q.get("totalVolume"),
                "open":         q.get("openPrice"),
                "prev_close":   q.get("closePrice"),
                "net_pct_chg":  q.get("netPercentChange"),
            }
    return out


def _clean(value, zero_is_missing=True):
    """Schwab reports unavailable numerics as 0.0 (e.g. P/E for unprofitable
    names, market cap for some ETFs) — store those as NULL."""
    if value is None:
        return None
    if zero_is_missing and value == 0:
        return None
    return float(value)


def get_schwab_fundamentals(symbols):
    """→ {symbol: {market_cap, shares_outstanding, avg_vol_10d_f,
                   pe_ratio, div_yield, beta}}
    Symbols Schwab doesn't know are simply absent from the result."""
    out = {}
    for i in range(0, len(symbols), FUND_BATCH):
        chunk = [_translate(s) for s in symbols[i:i + FUND_BATCH]]
        data  = _get(f"{MARKET_DATA}/instruments",
                     {"symbol": ",".join(chunk), "projection": "fundamental"})
        for inst in data.get("instruments", []):
            f   = inst.get("fundamental") or {}
            sym = _untranslate(inst.get("symbol", ""))
            if not sym:
                continue
            out[sym] = {
                "market_cap":         _clean(f.get("marketCap")),
                "shares_outstanding": _clean(f.get("sharesOutstanding")),
                "avg_vol_10d_f":      _clean(f.get("avg10DaysVolume")),
                "pe_ratio":           _clean(f.get("peRatio")),
                "div_yield":          _clean(f.get("dividendYield"), zero_is_missing=False),
                "beta":               _clean(f.get("beta")),
            }
    return out
