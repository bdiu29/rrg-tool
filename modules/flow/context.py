"""
Rule-6 confluence context per underlying — the trader's "agree with BX + structure +
discount" leg, assembled from the harness's existing signals (annotate + soft boost;
never a hard gate). Substitutes the trader's proprietary Monthly BX with:

  * sector_call   — the underlying's SPDR-sector RRG rotation call (BX analog)
  * regime        — the breadth market regime (HEALTHY / NEUTRAL / DETERIORATING)
  * vp_zone       — volume-profile value-area zone (discount / value / premium) =
                    the "Fair Value Bands / discount-vs-premium" leg
  * golden_pocket — price sitting in a Fibonacci golden pocket (structure/discount)

All pieces are fetched ONCE per poll for the whole universe and are fail-soft —
any unavailable signal is simply omitted, the rest still annotate the flow.
"""


def _sector_calls():
    """{sector_etf: rotation call} computed once (signal-cached). Fail-soft → {}."""
    try:
        from modules.rrg import compute_rrg, BENCHMARK, DEFAULT_TICKERS
        rrg = compute_rrg(DEFAULT_TICKERS, BENCHMARK, "1d")
        return {etf: d["call"] for etf, d in rrg["sectors"].items()}
    except Exception:
        return {}


def _sector_etf_for(symbol):
    try:
        from modules.schwab import _sector_etf
        return _sector_etf(symbol)
    except Exception:
        return None


def _regime():
    try:
        from modules.rrg import signal
        return signal.current_regime()
    except Exception:
        return None


def _vp_zones(symbols):
    """{symbol: volume-profile zone} from one cached OHLC fetch. Fail-soft → {}."""
    try:
        from modules.rrg import signal
        reads = signal.volume_profile_for(symbols)
        return {s: (r or {}).get("zone") for s, r in reads.items() if r}
    except Exception:
        return {}


def _golden_pockets(symbols):
    """{symbol: True} for symbols sitting in a golden pocket, from the screener
    snapshot (best-effort — only covers synced-universe names). Fail-soft → {}."""
    try:
        from modules.screener import store as scr_store
        snap = scr_store.get_snapshot()
        if snap is None or snap.empty or "gp_in_pocket" not in snap.columns:
            return {}
        out = {}
        for s in symbols:
            if s in snap.index:
                v = snap.loc[s, "gp_in_pocket"]
                if v == 1.0:
                    out[s] = True
        return out
    except Exception:
        return {}


def build_context(underlyings):
    """→ {underlying: {sector_call, regime, vp_zone, golden_pocket}} (omitting any
    unavailable key). Batched + fail-soft; safe to call every poll."""
    underlyings = sorted({s.upper() for s in underlyings if s})
    sector_calls = _sector_calls()
    regime = _regime()
    vp_zones = _vp_zones(underlyings)
    gps = _golden_pockets(underlyings)

    out = {}
    for s in underlyings:
        ctx = {}
        etf = _sector_etf_for(s)
        if etf and etf in sector_calls:
            ctx["sector_call"] = sector_calls[etf]
        if regime:
            ctx["regime"] = regime
        if vp_zones.get(s):
            ctx["vp_zone"] = vp_zones[s]
        if gps.get(s):
            ctx["golden_pocket"] = True
        out[s] = ctx
    return out
