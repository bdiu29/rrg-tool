"""
Rankings module — sector ETF relative-strength leaderboard.

A scannable companion to the RRG chart: the 11 SPDR sector ETFs scored 0-99 by
relative strength vs SPY, with the rank as of 1D/1W/1M ago, RS%/52w-high
columns, rank-up/down movers, and a top-stocks-per-sector drill-down.

The 0-99 rank is a *pooled historical percentile* of an RS composite (a
weighted blend of relative-strength returns over several horizons). Pooling the
composite across all 11 sectors over the full price history gives one reference
distribution; each sector's current composite maps to its percentile in it.
This is deliberately continuous (not an ordinal 1-of-11 ladder) so the values
spread non-uniformly and the day-over-day deltas the movers widgets read are
meaningful.

Reuses the RRG module's cached price path (`signal._fetch_close`) and its
RS-vs-SPY construction — no duplicate downloads. Top-stocks-per-sector reads
the screener's sector tags + RS snapshot (one documented cross-module read).

Routes:
  GET /rankings.html         → rankings.html
  GET /api/rankings          → full leaderboard payload (sectors + movers + leaders)
  GET /api/rankings/summary  → tiny payload for the hub badge
"""

import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from modules import Response
from modules.rrg import signal
from modules.rrg.signal import DEFAULT_TICKERS, SECTOR_NAMES, BENCHMARK

_MODULE_DIR = Path(__file__).resolve().parent

# RS composite: weighted blend of relative-strength returns over several
# horizons (trading days, weight). The pooled-historical percentile of this is
# the 0-99 rank. Tune here.
RS_LOOKBACKS = [(21, 0.50), (63, 0.30), (126, 0.20)]

# Rank-column / mover look-backs, in trading days.
LB_DAY   = 1
LB_WEEK  = 5
LB_MONTH = 21


# ---------------------------------------------------------------------------
# Rank math
# ---------------------------------------------------------------------------

def _rs_composite(close, tickers, benchmark):
    """DataFrame[date × ticker] of the RS composite — a weighted blend of
    relative-strength (ETF/benchmark) returns over RS_LOOKBACKS horizons."""
    bench = close[benchmark]
    comp = {}
    for t in tickers:
        if t not in close.columns:
            continue
        rs = close[t] / bench
        blend = sum(w * (rs / rs.shift(n) - 1) * 100 for n, w in RS_LOOKBACKS)
        comp[t] = blend
    return pd.DataFrame(comp)


def _percentile_mapper(reference):
    """Return fn: composite value → 0-99 pooled-historical percentile."""
    ref = np.sort(reference[~np.isnan(reference)])
    n = len(ref)

    def pctl(v):
        if v is None or not n or (isinstance(v, float) and np.isnan(v)):
            return None
        i = int(np.searchsorted(ref, v, side="right"))
        return int(min(99, max(0, round(100.0 * i / n))))

    return pctl


def _ret(series, n):
    """Percent return over the last n bars, or None if too short."""
    s = series.dropna()
    if len(s) <= n:
        return None
    return (s.iloc[-1] / s.iloc[-1 - n] - 1) * 100


def _rel(a, b):
    """Relative strength = a − b (both percents), rounded; None if either None."""
    if a is None or b is None:
        return None
    return round(a - b, 2)


def compute_rankings(tickers=DEFAULT_TICKERS, benchmark=BENCHMARK, close=None):
    """Build the sector leaderboard + rank movers. Returns a JSON-ready dict.

    `close` lets a caller inject a pre-built close panel (one column per
    `ticker` + `benchmark`) instead of fetching — used by the themes module to
    rank synthetic equal-weight theme indices. When None, prices are fetched."""
    if close is None:
        close = signal._fetch_close(list(tickers) + [benchmark], "1d", signal.PERIOD)
    if benchmark not in close.columns:
        raise ValueError("benchmark price unavailable")

    comp = _rs_composite(close, tickers, benchmark)
    if comp.empty:
        raise ValueError("no relative-strength data")
    pctl = _percentile_mapper(comp.to_numpy().ravel())
    bench = close[benchmark]
    date  = close.index[-1].strftime("%Y-%m-%d") if len(close.index) else None

    sectors = []
    for t in tickers:
        if t not in comp.columns:
            continue
        cseries = comp[t].dropna()
        if cseries.empty:
            continue

        def rank_at(lb):
            return pctl(cseries.iloc[-1 - lb]) if len(cseries) > lb else None

        price = close[t].dropna()
        if price.empty:
            continue
        last252 = price.tail(252)
        sectors.append({
            "ticker":       t,
            "name":         SECTOR_NAMES.get(t, t),
            "rank":         rank_at(0),
            "rank_1d":      rank_at(LB_DAY),
            "rank_1w":      rank_at(LB_WEEK),
            "rank_1m":      rank_at(LB_MONTH),
            "price":        round(float(price.iloc[-1]), 2),
            "rs_day":       _rel(_ret(close[t], LB_DAY),   _ret(bench, LB_DAY)),
            "rs_wk":        _rel(_ret(close[t], LB_WEEK),  _ret(bench, LB_WEEK)),
            "rs_mth":       _rel(_ret(close[t], LB_MONTH), _ret(bench, LB_MONTH)),
            "pct_52w_high": round((price.iloc[-1] / last252.max() - 1) * 100, 2),
        })

    sectors.sort(key=lambda s: (s["rank"] is not None, s["rank"] or 0), reverse=True)

    up_d, down_d = _rank_movers(sectors, "rank_1d")
    up_w, down_w = _rank_movers(sectors, "rank_1w")

    return {
        "date":            date,
        "benchmark":       benchmark,
        "sectors":         sectors,
        "rank_up_daily":   up_d,
        "rank_down_daily": down_d,
        "rank_up_weekly":  up_w,
        "rank_down_weekly": down_w,
    }


def _rank_movers(sectors, past_key, n=5):
    """(ups, downs): the n sectors whose rank rose / fell the most vs past_key."""
    moved = []
    for s in sectors:
        if s["rank"] is None or s[past_key] is None:
            continue
        moved.append({
            "ticker": s["ticker"], "name": s["name"],
            "rank":   s["rank"],   "price": s["price"],
            "delta":  s["rank"] - s[past_key],
        })
    ups   = sorted((m for m in moved if m["delta"] > 0),
                   key=lambda m: m["delta"], reverse=True)[:n]
    downs = sorted((m for m in moved if m["delta"] < 0),
                   key=lambda m: m["delta"])[:n]
    return ups, downs


def _pct(rate):
    """0–1 win rate → a rounded 0–100 percent, or None."""
    return None if rate is None else round(float(rate) * 100, 1)


def flag_stats_for(symbols):
    """{symbol: {bull_winrate, bull_n, bear_winrate, bear_n, exhaustion}} — each
    name's own historical flag reliability (from the screener's background-
    precomputed, ~90-day-cached table) plus its current volume-exhaustion state.
    Shared by rankings + themes. Fail-soft → {} if the screener isn't available;
    kicks the (incremental) precompute in the background if it hasn't run today."""
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    try:
        from modules.screener import store as screener_store, snapshot as screener_snapshot
    except Exception:
        return {}
    try:
        if screener_snapshot.needs_flagstats_refresh():
            screener_snapshot.start_refresh("flagstats")   # background, non-blocking
    except Exception:
        pass
    wr  = {}
    sig = {}
    try:
        wr = screener_store.get_flag_winrates(symbols)
    except Exception:
        wr = {}
    try:
        sig = screener_store.fetch_signal_states(symbols)
    except Exception:
        sig = {}
    out = {}
    for s in symbols:
        w  = wr.get(s, {})
        ex = (sig.get(s) or {}).get("exhaustion")
        out[s] = {
            "bull_winrate": _pct(w.get("bull_rate")), "bull_n": w.get("bull_n") or 0,
            "bear_winrate": _pct(w.get("bear_rate")), "bear_n": w.get("bear_n") or 0,
            "exhaustion":   ex if ex in ("buyer", "seller") else None,
        }
    return out


def _sector_leaders(n=15):
    """Top-n RS-ranked stocks per SPDR sector, read from the screener store, each
    enriched with its flag win-rate + current volume exhaustion. Fail-soft: any
    sector with no synced fundamentals/snapshot returns []."""
    try:
        from modules.screener import store as screener_store
    except Exception:
        return {}
    out = {}
    for t in DEFAULT_TICKERS:
        try:
            out[t] = screener_store.fetch_sector_leaders(t, n)
        except Exception:
            out[t] = []
    all_syms = [r["symbol"] for rows in out.values() for r in rows]
    stats = flag_stats_for(all_syms)
    for rows in out.values():
        for r in rows:
            r.update(stats.get(r["symbol"], {}))
    return out


# Actual ETF top holdings (the other half of the leaders toggle). yfinance
# returns ~10 holdings with weights; we enrich them with the price data the
# screener already has. Holdings barely move day to day → cache for hours.
_HOLDINGS_CACHE = {}
_HOLDINGS_TTL   = 6 * 3600


def fetch_holdings(etf, n=15):
    """The ETF's real top holdings (symbol/name/weight%) by index weight,
    enriched with snapshot price/chg where available. Fail-soft → []."""
    hit = _HOLDINGS_CACHE.get(etf)
    if hit and time.time() - hit[0] < _HOLDINGS_TTL:
        return hit[1]

    import yfinance as yf
    rows = []
    try:
        th = yf.Ticker(etf).funds_data.top_holdings
        for sym, r in th.iterrows():
            rows.append({
                "symbol": str(sym),
                "name":   r.get("Name"),
                "weight": round(float(r.get("Holding Percent") or 0) * 100, 2),
            })
            if len(rows) >= n:
                break
    except Exception:
        return []          # don't cache a transient fetch failure

    if not rows:
        return []
    try:
        from modules.screener import store as screener_store
        quotes = screener_store.fetch_quotes([x["symbol"] for x in rows])
        for x in rows:
            q = quotes.get(x["symbol"], {})
            x["close"]   = q.get("close")
            x["chg_pct"] = q.get("chg_pct")
    except Exception:
        pass

    _HOLDINGS_CACHE[etf] = (time.time(), rows)
    return rows


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_index(req):
    with open(_MODULE_DIR / "rankings.html") as f:
        return Response.html(f.read())


def _handle_rankings(req):
    try:
        result = compute_rankings()
        result["leaders"] = _sector_leaders()
        result["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return Response.json(result)
    except Exception as e:
        return Response.error(str(e))


def _handle_holdings(req):
    etf = (req.qs.get("sector", [""])[0] or "").upper()
    if etf not in DEFAULT_TICKERS:
        return Response.error("unknown sector", 400)
    try:
        return Response.json({"sector": etf, "holdings": fetch_holdings(etf)})
    except Exception as e:
        return Response.error(str(e))


def _handle_summary(req):
    try:
        sectors = compute_rankings()["sectors"]
        top = sectors[0] if sectors else None
        if not top or top["rank"] is None:
            return Response.json({"status": "neutral", "text": "no data"})
        return Response.json({
            "status": "healthy",
            "leader": top["ticker"],
            "rank":   top["rank"],
            "text":   f"{top['ticker']} #1 · rank {top['rank']}",
        })
    except Exception as e:
        return Response.error(str(e))


def register_routes(router):
    router.get("/rankings.html",         _handle_index)
    router.get("/api/rankings",          _handle_rankings)
    router.get("/api/rankings/holdings", _handle_holdings)
    router.get("/api/rankings/summary",  _handle_summary)
