"""
Stock-pick suggestion engine — the harness confluence at the STOCK level.

The user uploads a watchlist; this scores each name on TWO axes and suggests the
ones that have a strong **impulsive-move setup** AND are **solid enough to hold** if
the move fails (the explicit ask — setups on quality names, not momentum lottos):

  • IMPULSE (0-100) — setup confluence from the screener snapshot fields
    (bull flag, golden pocket, breakout vs 20d/52w highs, RVOL / volume-building,
    accumulation A/D rating, RS leadership, momentum) minus a buying-climax penalty.
  • HOLD-ABILITY (0-100) — the CANSLIM growth/quality score when fundamentals exist
    (else an RS+trend proxy), BLENDED with the research module's per-ticker
    fundamental-conviction score (valuation + trend + self-contained growth/demand/
    sponsorship/RS; weight FUND_BLEND), times a quality FLOOR (penny price /
    illiquidity / broken-downtrend penalties) — i.e. "would I be OK holding this if
    it dips?". The fundamental read is the deep-dive's number folded in here so
    conviction on a name flows straight into its size; fail-soft (unchanged when the
    research module is unavailable).
  • PICK = geomean(impulse, hold) × the market-regime factor, with an event/earnings
    damper. A great setup on a junk stock scores LOW (geomean punishes a weak axis);
    a great stock with no setup isn't a buy-now. `tradeable` requires BOTH axes pass.

This is composition, not new quant (the CANSLIM / themes precedent): every input is a
signal the app already produces. `score_symbol` is pure (a row dict + a context dict)
so it unit-tests offline; `_rows` does the hybrid fetch (screener snapshot when a
symbol is in it, else yfinance on demand — so ANY uploaded watchlist works). The stop
(close − k·ATR) is the downside plan the paper engine (Layer B) trades against.
"""

import math
import time

from modules.harness import store

_SUGGEST_TTL = 15 * 60
_SUGGEST_CACHE = {"key": None, "at": 0.0, "result": None}

# Gates / sizing — judgment-set like the rest of the project's confluence weights.
MIN_HOLD     = 45.0     # below this a name isn't "solid to hold" → not tradeable
MIN_IMPULSE  = 40.0     # below this there's no actionable setup → not a buy-now
FUND_BLEND   = 0.45     # weight of the research module's fundamental score in HOLD (when present)
ATR_STOP_K   = 2.0      # stop = close − K·ATR14 (target = 2× that distance up; 2:1)
PRICE_FLOOR  = 5.0      # sub-$5 = not a "solid hold"
DOLLAR_VOL_FLOOR = 3e6  # < $3M/day average = too illiquid to hold confidently

_REGIME_FACTOR = {"HEALTHY": 1.0, "NEUTRAL": 0.9, "DETERIORATING": 0.75}

# Impulse setup weights (summed, then clamped 0-100).
_W = {
    "flag_bull": 25.0, "flag_bear": -15.0,
    "gp_in": 20.0, "gp_approach": 10.0,
    "breakout_52w": 20.0, "breakout_20d": 12.0,
    "rvol_3x": 20.0, "rvol_2x": 12.0, "vol_building": 8.0,
    "ad_A": 15.0, "ad_B": 8.0, "ad_DE": -10.0,
    "rs_leader": 10.0, "momentum": 8.0,
    "buying_climax": -20.0,
}


def _f(x):
    """→ float, or None for missing / NaN."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _str(x):
    """→ the string, or '' for NaN / float / None. (NaN is truthy, so the `x or ""`
    idiom is unsafe for snapshot string columns that come back NaN on stale names.)"""
    return x if isinstance(x, str) else ""


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# The two axes
# ---------------------------------------------------------------------------

def _impulse(row):
    """Setup confluence → (0-100, factors[])."""
    s, why = 0.0, []

    def add(amt, label):
        nonlocal s
        if amt:
            s += amt
            why.append((amt, label))

    flag = _str(row.get("flag")).lower()
    if flag == "bull":   add(_W["flag_bull"], "bull flag")
    elif flag == "bear": add(_W["flag_bear"], "bear flag")

    if _f(row.get("gp_in_pocket")):       add(_W["gp_in"], "in golden pocket")
    elif _f(row.get("gp_approaching")):   add(_W["gp_approach"], "approaching golden pocket")

    close, h20, h252 = _f(row.get("close")), _f(row.get("high_20d")), _f(row.get("high_252"))
    pfh = _f(row.get("pct_from_52w_high"))
    if pfh is not None and pfh >= -2:     add(_W["breakout_52w"], "at/near 52w high")
    elif close and h20 and close >= h20:  add(_W["breakout_20d"], "20d breakout")

    rvol = _f(row.get("rvol_10d")); chg = _f(row.get("chg_pct"))
    if rvol is not None:
        if rvol >= 3:    add(_W["rvol_3x"], f"RVOL {rvol:.1f}×")
        elif rvol >= 2:  add(_W["rvol_2x"], f"RVOL {rvol:.1f}×")
        elif rvol >= 1.5 and chg is not None and abs(chg) < 3:
            add(_W["vol_building"], "volume building")

    ad = _str(row.get("ad_rating")).upper()
    if ad == "A":            add(_W["ad_A"], "strong accumulation")
    elif ad == "B":          add(_W["ad_B"], "accumulation")
    elif ad in ("D", "E"):   add(_W["ad_DE"], "distribution")

    rs1, rs3 = _f(row.get("rs_1m_pct")), _f(row.get("rs_3m_pct"))
    if rs1 is not None and rs3 is not None and rs1 > 0 and rs3 > 0:
        add(_W["rs_leader"], "RS leader")

    c, e20, e50 = close, _f(row.get("ema20")), _f(row.get("ema50"))
    if c and e20 and e50 and c > e20 > e50:
        add(_W["momentum"], "momentum (>20/50 EMA)")

    if _str(row.get("exhaustion")).lower() == "buyer":
        add(_W["buying_climax"], "buying climax (late)")

    why.sort(key=lambda f: abs(f[0]), reverse=True)
    return _clamp(s), [lbl for _, lbl in why]


def _canslim_quality(row):
    """CANSLIM composite_raw (0-100) when fundamentals exist, else None. Pure."""
    try:
        from modules.canslim import score_stock
        card = score_stock(row, row.get("_rs_rating"), row.get("_float_pctl"),
                            row.get("_inst_read"))
        return card.get("composite_raw")
    except Exception:
        return None


def _hold(row):
    """Hold-ability → (0-100, factors[], canslim_composite|None)."""
    why = []
    canslim = _canslim_quality(row)
    if canslim is not None:
        quality = canslim
        why.append(f"CANSLIM {canslim:.0f}")
    else:
        # proxy: RS percentile + trend health, centered at 50 (growth unknown)
        quality = 50.0
        rr = _f(row.get("_rs_rating"))
        if rr is not None:
            quality += (rr - 50) * 0.4
        c, s50, s200 = _f(row.get("close")), _f(row.get("sma50")), _f(row.get("sma200"))
        if c and s50 and s200:
            if c > s50 > s200:
                quality += 15; why.append("stage-2 uptrend")
            elif c < s200:
                quality -= 15
        quality = _clamp(quality)
        why.append(f"quality≈{quality:.0f} (no fundamentals)")

    # fold in the research module's fundamental-conviction score (the per-ticker
    # deep-dive's number, attached upstream in suggest()) — it adds valuation/
    # trend/self-contained growth the CANSLIM/proxy read alone doesn't capture.
    fund = _f(row.get("_fund_score"))
    if fund is not None:
        quality = FUND_BLEND * fund + (1.0 - FUND_BLEND) * quality
        why.append(f"fundamentals {fund:.0f}")

    # quality FLOOR — the "solid to hold" hard checks (multiplicative)
    floor = 1.0
    close = _f(row.get("close"))
    avgvol = _f(row.get("avg_vol_10d"))
    if close is not None and close < PRICE_FLOOR:
        floor *= 0.5; why.append("sub-$5 price")
    if close and avgvol and close * avgvol < DOLLAR_VOL_FLOOR:
        floor *= 0.6; why.append("thin liquidity")
    s200 = _f(row.get("sma200")); rs1 = _f(row.get("rs_1m_pct"))
    if close and s200 and close < s200 and rs1 is not None and rs1 < 0:
        floor *= 0.7; why.append("below 200d & lagging")

    return _clamp(quality * floor), why, canslim


# ---------------------------------------------------------------------------
# The combined pick
# ---------------------------------------------------------------------------

def score_symbol(row, ctx=None):
    """Score one symbol-row → the suggestion dict. Pure (no I/O). `ctx` carries the
    market-regime factor + event-risk flag + per-symbol earnings-soon flag."""
    ctx = ctx or {}
    impulse, imp_why = _impulse(row)
    hold, hold_why, canslim = _hold(row)

    pick = math.sqrt(impulse * hold)               # geomean — both axes must be decent
    pick *= float(ctx.get("regime_factor", 1.0))

    damper, dwhy = 1.0, None
    if ctx.get("event_risk") or row.get("_earnings_soon"):
        damper = 0.85
        dwhy = "size smaller — event/earnings risk into a print"
    pick = _clamp(pick * damper)

    close = _f(row.get("close")); atr = _f(row.get("atr14"))
    stop = target = risk_pct = None
    if close and atr:
        stop = round(close - ATR_STOP_K * atr, 2)
        target = round(close + 2 * ATR_STOP_K * atr, 2)
        risk_pct = round((close - stop) / close * 100, 1)

    tradeable = hold >= MIN_HOLD and impulse >= MIN_IMPULSE
    if not tradeable:
        if hold < MIN_HOLD:
            reason = "quality too low to hold"
        else:
            reason = "no actionable setup yet"
    else:
        reason = None

    why = imp_why[:3] + hold_why[:2]
    if dwhy:
        why.append(dwhy)

    return {
        "symbol":   row.get("symbol"),
        "pick":     round(pick, 1),
        "impulse":  round(impulse, 1),
        "hold":     round(hold, 1),
        "tradeable": tradeable,
        "reason":   reason,
        "close":    close,
        "stop":     stop,
        "target":   target,
        "risk_pct": risk_pct,
        "canslim":  None if canslim is None else round(canslim),
        "fund_score":   _f(row.get("_fund_score")),
        "fund_verdict": _str(row.get("_fund_verdict")) or None,
        "flag":     _str(row.get("flag")) or None,
        "ad_rating": _str(row.get("ad_rating")) or None,
        "rs_1m_pct": _f(row.get("rs_1m_pct")),
        "sector":   _str(row.get("sector")) or None,
        "source":   row.get("_source", "snapshot"),
        "why":      why,
    }


# ---------------------------------------------------------------------------
# Data (hybrid: screener snapshot when present, else yfinance on demand)
# ---------------------------------------------------------------------------

_FUND_CACHE = {}


def _yf_info(sym):
    """Best-effort fundamentals for an off-universe ticker (cached, fail-soft)."""
    try:
        import yfinance as yf
        info = yf.Ticker(sym).info or {}
    except Exception:
        return {}
    eq, ea = info.get("earningsQuarterlyGrowth"), info.get("earningsGrowth")
    pct = lambda v: round(v * 100, 1) if isinstance(v, (int, float)) else None
    return {
        "shares_outstanding": info.get("sharesOutstanding"),
        "beta": info.get("beta"),
        "eps_growth_q": pct(eq), "eps_growth_a": pct(ea),
        "sector": info.get("sector"),
        "inst_pct_held": info.get("heldPercentInstitutions"),
    }


def _fundamentals(symbols):
    """{sym: {fundamental fields}} — screener snapshot first, else yfinance .info."""
    fdf = None
    try:
        from modules.screener import store as scr_store
        fdf = scr_store.get_fundamentals()
    except Exception:
        fdf = None
    cols = list(fdf.columns) if fdf is not None else []
    keep = [c for c in ("shares_outstanding", "beta", "eps_growth_q", "eps_growth_a",
                        "sector", "inst_pct_held", "earnings_date") if c in cols]
    out = {}
    for s in symbols:
        if fdf is not None and s in fdf.index:
            r = fdf.loc[s]
            out[s] = {k: (None if r.get(k) is None else r.get(k)) for k in keep}
            out[s]["_source"] = "snapshot"
        else:
            if s not in _FUND_CACHE:
                _FUND_CACHE[s] = _yf_info(s)
            out[s] = dict(_FUND_CACHE[s])
            out[s]["_source"] = "on-demand"
    return out


def _rows(symbols):
    """Build per-symbol rows (setup fields via compute_snapshot + fundamentals).
    Hybrid + fail-soft. Returns {sym: row}."""
    from modules.rrg import signal
    from modules.screener import metrics
    syms = [s for s in symbols if s]
    if not syms:
        return {}
    fetch = syms + (["SPY"] if "SPY" not in syms else [])
    try:
        ohlc = signal.fetch_ohlc(fetch)
    except Exception:
        return {}
    close = ohlc.get("close")
    if close is None or getattr(close, "empty", True):
        return {}
    spy = close["SPY"] if "SPY" in close.columns else close.iloc[:, 0]
    snap = metrics.compute_snapshot(close, ohlc["volume"], ohlc["open"],
                                    ohlc["high"], ohlc["low"], spy)
    if snap is None or snap.empty:
        return {}
    snap = snap.set_index("symbol")
    # keep only names that actually resolved with a price (skips indices/crypto/foreign
    # tickers yfinance can't price — and so avoids a slow .info call on each of them).
    present = [s for s in syms
               if s in snap.index and _f(snap.loc[s].get("close")) is not None]
    funda = _fundamentals(present)
    rows = {}
    for s in present:
        r = {k: snap.loc[s][k] for k in snap.columns}
        f = funda.get(s, {})
        r.update(f)
        r["symbol"] = s
        r["_source"] = f.get("_source", "snapshot")
        rows[s] = r
    return rows


# ---------------------------------------------------------------------------
# Market context + the public suggest()
# ---------------------------------------------------------------------------

def _market_ctx():
    regime = None
    try:
        from modules.rrg import signal
        regime = signal.current_regime()
    except Exception:
        pass
    event = False
    try:
        from modules.news import calendar as news_cal
        er = news_cal.event_risk()
        event = bool(er and er.get("flag"))
    except Exception:
        pass
    reg = (regime or "NEUTRAL").upper()
    return {"regime": reg, "regime_factor": _REGIME_FACTOR.get(reg, 0.9),
            "event_risk": event}


def _attach_cross_sectional(rows):
    """Cross-sectional RS rating + float percentile across the watchlist (for CANSLIM)."""
    try:
        from modules.rankings import _percentile_mapper
    except Exception:
        _percentile_mapper = None
    import numpy as np
    rs_vals = {}
    for s, r in rows.items():
        parts = [_f(r.get("rs_1m_pct")), _f(r.get("rs_3m_pct"))]
        parts = [p for p in parts if p is not None]
        rs_vals[s] = sum(parts) / len(parts) if parts else None
    so_vals = {s: _f(r.get("shares_outstanding")) for s, r in rows.items()}
    rs_map = so_map = (lambda v: None)
    if _percentile_mapper is not None:
        arr = np.array([v for v in rs_vals.values() if v is not None], dtype=float)
        if arr.size:
            rs_map = _percentile_mapper(arr)
        so_arr = np.array([v for v in so_vals.values() if v is not None], dtype=float)
        if so_arr.size:
            so_map = _percentile_mapper(so_arr)
    for s, r in rows.items():
        r["_rs_rating"] = rs_map(rs_vals[s]) if rs_vals[s] is not None else None
        r["_float_pctl"] = so_map(so_vals[s]) if so_vals[s] is not None else None


def _attach_fundamentals(rows):
    """Attach the research module's per-ticker fundamental-conviction score to each
    row (read by _hold's HOLD blend + surfaced in the suggestion). Pure scorer, fail-
    soft: if research is unavailable the rows are unchanged and HOLD is the old math."""
    try:
        from modules.research import fundamental_score
    except Exception:
        return
    for r in rows.values():
        try:
            fs = fundamental_score(r)
        except Exception:
            fs = None
        r["_fund_score"] = (fs or {}).get("score")
        r["_fund_verdict"] = (fs or {}).get("verdict")


def suggest(symbols=None, ctx=None, use_cache=True):
    """Rank the watchlist by the impulse×hold pick score. Fail-soft → empty list.
    Cached ~15 min keyed on the watchlist set (the fetch+score is the slow part), so
    repeat UI loads and the chat's grounding are instant; `use_cache=False` forces a
    fresh compute."""
    symbols = symbols if symbols is not None else store.get_watchlist()
    key = tuple(sorted(symbols))
    if (use_cache and _SUGGEST_CACHE["key"] == key and _SUGGEST_CACHE["result"]
            and time.time() - _SUGGEST_CACHE["at"] < _SUGGEST_TTL):
        return _SUGGEST_CACHE["result"]

    ctx = ctx or _market_ctx()
    rows = _rows(symbols)
    if not rows:
        return {"as_of": None, "count": 0, "ctx": ctx, "suggestions": [],
                "note": "No data — upload a watchlist (and the names must resolve on yfinance)."}
    _attach_cross_sectional(rows)
    _attach_fundamentals(rows)
    sugg = [score_symbol(r, ctx) for r in rows.values()]
    sugg.sort(key=lambda x: x["pick"], reverse=True)
    as_of = None
    for r in rows.values():
        d = r.get("date")
        if d:
            as_of = str(d)
            break
    result = {
        "as_of": as_of, "count": len(sugg), "ctx": ctx, "suggestions": sugg,
        "tradeable": sum(1 for s in sugg if s["tradeable"]),
        "note": ("Impulse = setup confluence; Hold = CANSLIM quality blended with the "
                 "research fundamental score, × a liquidity/price/trend floor; Pick = "
                 "geomean(impulse, hold) × regime. Stops are 2×ATR. Off-universe names use "
                 "on-demand yfinance fundamentals (best-effort)."),
    }
    _SUGGEST_CACHE.update(key=key, at=time.time(), result=result)
    return result


def cached_suggestions():
    """Last computed suggestions if still fresh, else None — for chat grounding (never
    triggers a fetch)."""
    if _SUGGEST_CACHE["result"] and time.time() - _SUGGEST_CACHE["at"] < _SUGGEST_TTL:
        return _SUGGEST_CACHE["result"]
    return None
