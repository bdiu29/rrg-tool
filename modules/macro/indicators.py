"""
The named "signals of health" — a Leading-Indicators panel (market internals:
yield curve, credit, sector leadership, breadth, VIX, momentum, small caps, tech)
and a Macro-Indicators panel (highs-lows, A/D, copper/gold, vol spread, curve,
VIX term structure, jobless claims, McClellan).

Each row carries a VALUE, a 20-bar Δ, a STATE, and a plain-english MEANING — the
meaning is a deterministic template per (indicator, state), so the panel is instant
and $0 (the LLM is reserved for the top-of-page interpretation). The same raw inputs
also produce the growth / inflation feature z-scores `regime.py` turns into the
4-quadrant probabilities — computed once, read twice.

States (mirrors the Livermore vocabulary, mapped to this project's palette):
  STABLE      — healthy / normal               (green)
  COMPLACENT  — too-good / stretched, risk building (amber)
  WATCH       — mild caution, deteriorating     (amber)
  TURNED      — flipped / extreme reading        (red)
"""

import numpy as np

from modules.macro import sources as src

STABLE, COMPLACENT, WATCH, TURNED = "STABLE", "COMPLACENT", "WATCH", "TURNED"
_CAUTION = (COMPLACENT, WATCH, TURNED)


def _row(key, group, label, value, unit, delta, delta_unit, state, meaning):
    return {"key": key, "group": group, "label": label,
            "value": None if value is None else round(float(value), 3),
            "unit": unit, "delta": None if delta is None else round(float(delta), 2),
            "delta_unit": delta_unit, "state": state, "meaning": meaning}


def _realized_vol(spy, n=20):
    """Annualized realized vol (%) of SPY daily returns — the denominator of the
    implied-vs-realized 'vol spread'."""
    if spy is None or len(spy) < n + 1:
        return None
    rets = np.log(spy / spy.shift(1)).dropna().iloc[-n:]
    if len(rets) < n:
        return None
    return float(rets.std() * np.sqrt(252) * 100)


def _fred_level_delta(fred, sid, n=20):
    """(latest level, n-obs change) for a FRED series, or (None, None)."""
    s = fred.get(sid)
    if s is None or len(s) == 0:
        return None, None
    v = float(s.iloc[-1])
    d = (v - float(s.iloc[-1 - n])) if len(s) > n else None
    return v, d


# ---------------------------------------------------------------------------
# Leading indicators (market internals)
# ---------------------------------------------------------------------------

def _leading(raw):
    close, fred = raw["close"], raw["fred"]
    b = (raw.get("breadth") or {}).get("metrics") or {}
    rows = []

    # 1 — Yield curve (10Y-2Y). Inversion = the classic late-cycle warning.
    lvl, d = _fred_level_delta(fred, "T10Y2Y")
    if lvl is not None:
        if lvl < 0:
            st, m = TURNED, "The yield curve is inverted — a classic late-cycle recession warning."
        elif lvl < 0.25:
            st, m = WATCH, "The curve is very flat. Late-cycle — watch for it tipping into inversion."
        else:
            st, m = STABLE, "Interest rates look normal. No recession signal from the curve right now."
        rows.append(_row("yield_curve", "leading", "Yield Curve", lvl, "pp",
                         None if d is None else d * 100, "bps", st, m))

    # 2 — Credit spreads (high-yield OAS). The bond market's stress gauge.
    lvl, d = _fred_level_delta(fred, "BAMLH0A0HYM2")
    if lvl is not None:
        rising = d is not None and d > 0.5
        if lvl > 5.0:
            st, m = TURNED, "Credit stress is rising — the bond market is pricing real risk in corporate debt."
        elif lvl > 3.75 or rising:
            st, m = WATCH, "Credit spreads are widening a touch. Worth watching, not yet alarming."
        else:
            st, m = STABLE, "Bond markets are calm. No stress signals in corporate debt."
        rows.append(_row("credit_spreads", "leading", "Credit Spreads", lvl, "%",
                         None if d is None else d * 100, "bps", st, m))

    # 3 — Sector leadership: growth basket vs defensive basket. A stretched gap is
    #     complacency — vulnerable to a snap-back rotation.
    g, dfn = src.basket(close, ["XLK", "XLY", "XLC"]), src.basket(close, ["XLU", "XLP", "XLV"])
    if g is not None and dfn is not None:
        rel = (g / dfn).dropna()
        spread63 = src.pct_change_n(rel, 63)
        z = src.zscore_mom(rel, win=20)
        d20 = src.pct_change_n(rel, 20)
        if z is not None and z > 1.0:
            st, m = COMPLACENT, "Growth sectors are far ahead of defensives. The gap is stretched and vulnerable to a snap-back rotation."
        elif z is not None and z < -1.0:
            st, m = WATCH, "Defensives are taking the lead — a risk-off tell beneath the surface."
        else:
            st, m = STABLE, "Sector leadership is balanced. No dangerous crowding into one end of the market."
        rows.append(_row("sector_leadership", "leading", "Sector Leadership",
                         spread63, "%", d20, "%", st, m))

    # 4 — Market breadth: % of stocks above their 50d average.
    p50 = b.get("pct_above_50")
    if p50 is not None:
        if p50 >= 60:
            st, m = STABLE, "Most stocks are participating in the move. Broad participation is healthy."
        elif p50 >= 45:
            st, m = WATCH, "Only about half of stocks are above their 50-day line — participation is middling."
        else:
            st, m = TURNED, "Fewer than half of stocks are holding their 50-day line — the move is narrow and fragile."
        rows.append(_row("market_breadth", "leading", "Market Breadth", p50, "%",
                         None, "", st, m))

    # 5 — VIX. Very low = complacency (corrections often start here); spiking = fear.
    vix = src.col(close, "^VIX")
    if vix is not None:
        v = float(vix.iloc[-1]); d = src.pct_change_n(vix, 20)
        dpts = (v - float(vix.iloc[-21])) if len(vix) > 21 else None
        if v < 14:
            st, m = COMPLACENT, "Fear is very low. Markets are calm — possibly too calm. Corrections often start from here."
        elif v > 28:
            st, m = TURNED, "Volatility is spiking. Fear is elevated and the tape is risk-off."
        elif v > 20:
            st, m = WATCH, "Volatility is creeping up. Stay nimble — the calm is fraying."
        else:
            st, m = STABLE, "Volatility is in a normal range. Orderly two-way trade."
        rows.append(_row("volatility_vix", "leading", "Volatility (VIX)", v, "",
                         dpts, "pts", st, m))

    # 6 — Market momentum: how far SPY sits above its 200d line, and 20d trend.
    spy = src.col(close, "SPY")
    if spy is not None and len(spy) > 200:
        ma200 = float(spy.rolling(200).mean().iloc[-1])
        above = (float(spy.iloc[-1]) / ma200 - 1) * 100
        d20 = src.pct_change_n(spy, 20)
        if above < 0:
            st, m = TURNED, "The S&P 500 has slipped below its 200-day trend — the primary uptrend is in question."
        elif above > 15:
            st, m = COMPLACENT, "The S&P 500 is stretched well above its trend. Healthy, but a pause or pullback is overdue."
        else:
            st, m = STABLE, "The S&P 500 trend is intact and healthy. No overextension."
        rows.append(_row("market_momentum", "leading", "Market Momentum", above, "%",
                         d20, "%", st, m))

    # 7 — Small caps (IWM vs SPY). Hot small caps = risk appetite at complacent extremes.
    iwm = src.ratio(close, "IWM", "SPY")
    if iwm is not None:
        spread63 = src.pct_change_n(iwm, 63); z = src.zscore_mom(iwm, win=20)
        d20 = src.pct_change_n(iwm, 20)
        if z is not None and z > 1.0:
            st, m = COMPLACENT, "Small caps are running hot. Enthusiasm for the riskiest names can mark a market top."
        elif z is not None and z < -1.0:
            st, m = WATCH, "Small caps are lagging badly — risk appetite is draining out of the tape."
        else:
            st, m = STABLE, "Small caps are roughly keeping pace. Risk appetite is steady."
        rows.append(_row("small_caps", "leading", "Small Caps", spread63, "%",
                         d20, "%", st, m))

    # 8 — Tech leadership (XLK vs SPY). Extreme tech crowding = concentration risk.
    xlk = src.ratio(close, "XLK", "SPY")
    if xlk is not None:
        spread63 = src.pct_change_n(xlk, 63); z = src.zscore_mom(xlk, win=20)
        d20 = src.pct_change_n(xlk, 20)
        if z is not None and z > 1.2:
            st, m = COMPLACENT, "Tech is pulling far ahead of the market — concentration risk is building."
        else:
            st, m = STABLE, "Tech is roughly in line with the broader market. No concentration risk."
        rows.append(_row("tech_leadership", "leading", "Tech Leadership", spread63, "%",
                         d20, "%", st, m))

    return rows


# ---------------------------------------------------------------------------
# Macro indicators
# ---------------------------------------------------------------------------

def _macro(raw):
    close, fred = raw["close"], raw["fred"]
    b = (raw.get("breadth") or {}).get("metrics") or {}
    rows = []

    # 1 — New highs vs lows.
    nh_nl = b.get("nh_nl")
    if nh_nl is not None:
        if nh_nl >= 2:
            st, m = STABLE, "New highs comfortably outnumber new lows. Healthy participation."
        elif nh_nl >= 1:
            st, m = WATCH, "New highs are barely outpacing new lows. Participation is fading — be selective."
        else:
            st, m = TURNED, "New lows are overtaking new highs — the internals are rolling over."
        rows.append(_row("new_highs_lows", "macro", "New Highs vs Lows", nh_nl, "x",
                         None, "", st, m))

    # 2 — Advance / decline (counts come from build_summary's metrics).
    adv, dec = b.get("advances"), b.get("declines")
    if adv is not None and dec is not None and dec > 0:
        ad = adv / dec
        if ad >= 1.0:
            st, m = STABLE, f"{int(adv)} advancing vs {int(dec)} declining — more stocks rising than falling."
        elif ad >= 0.7:
            st, m = WATCH, f"{int(adv)} advancing vs {int(dec)} declining — more stocks falling than rising."
        else:
            st, m = TURNED, f"{int(adv)} advancing vs {int(dec)} declining — broad, heavy selling."
        rows.append(_row("advance_decline", "macro", "Advance / Decline", ad, "x",
                         None, "", st, m))

    # 3 — Copper / gold (Dr. Copper vs the fear metal). Rolling over = growth scare.
    copper = src.col_any(close, "HG=F", "CPER")
    gold   = src.col_any(close, "GC=F", "GLD")
    if copper is not None and gold is not None:
        cg = (copper / gold).dropna()
        if len(cg) > 21:
            d20 = src.pct_change_n(cg, 20)
            if d20 is not None and d20 < -4:
                st, m = WATCH, "Copper is rolling over against gold — an early growth-scare / recession tell."
            elif d20 is not None and d20 > 4:
                st, m = STABLE, "Copper is leading gold — the cyclical, reflationary read. No recession signal."
            else:
                st, m = STABLE, "Copper/gold is steady. No recession signal from the commodity complex."
            rows.append(_row("copper_gold", "macro", "Copper / Gold Ratio",
                             float(cg.iloc[-1]), "", d20, "%", st, m))

    # 4 — Volatility spread: implied (VIX) minus realized.
    vix, spy = src.col(close, "^VIX"), src.col(close, "SPY")
    rv = _realized_vol(spy)
    if vix is not None and rv is not None:
        iv = float(vix.iloc[-1]); spread = iv - rv
        if spread > 7:
            st, m = WATCH, f"Implied vol ({iv:.1f}) sits well above realized ({rv:.1f}). Fear is elevated relative to actual moves."
        elif spread < -2:
            st, m = COMPLACENT, "Realized vol is running above implied — hedging looks complacent into real movement."
        else:
            st, m = STABLE, "Implied and realized volatility are roughly aligned. Risk is being priced sensibly."
        rows.append(_row("volatility_spread", "macro", "Volatility Spread", spread, "pts",
                         None, "", st, m))

    # 5 — 10Y-2Y curve level (the macro framing of the same FRED spread).
    lvl, _ = _fred_level_delta(fred, "T10Y2Y")
    if lvl is not None:
        if lvl < 0:
            st, m = TURNED, f"The 10Y-2Y curve is inverted ({lvl:+.2f}%) — a recession warning that historically leads by quarters."
        else:
            st, m = STABLE, f"The yield curve is normal at {lvl:+.2f}%. No recession warning."
        rows.append(_row("yield_10y2y", "macro", "10Y-2Y Treasury Spread", lvl, "pp",
                         None, "", st, m))

    # 6 — VIX term structure (spot vs 3-month). Backwardation = acute near-term stress.
    v1, v3 = src.col(close, "^VIX"), src.col(close, "^VIX3M")
    if v1 is not None and v3 is not None and float(v3.iloc[-1]) > 0:
        a, c = float(v1.iloc[-1]), float(v3.iloc[-1]); ts = a / c
        if ts >= 1.0:
            st, m = TURNED, f"VIX is backwardated (spot {a:.1f} above 3-month {c:.1f}) — acute near-term stress."
        else:
            st, m = STABLE, f"VIX is in normal contango ({a:.1f} spot vs {c:.1f} 3-month). Orderly risk pricing."
        rows.append(_row("vix_term_structure", "macro", "VIX Term Structure", ts, "x",
                         None, "", st, m))

    # 7 — Initial jobless claims (the highest-frequency labor read).
    s = fred.get("ICSA")
    if s is not None and len(s):
        k = float(s.iloc[-1]) / 1000.0
        d = (float(s.iloc[-1]) - float(s.iloc[-1 - 4])) / 1000.0 if len(s) > 4 else None
        if k < 260:
            st, m = STABLE, f"Jobless claims at {k:.0f}K. The labor market is healthy."
        elif k < 300:
            st, m = WATCH, f"Jobless claims at {k:.0f}K and drifting up — the labor market is loosening."
        else:
            st, m = TURNED, f"Jobless claims at {k:.0f}K — labor-market stress is showing up in the data."
        rows.append(_row("jobless_claims", "macro", "Jobless Claims", k, "K",
                         d, "K", st, m))

    # 8 — McClellan oscillator (breadth momentum). Extremes flag washouts / blowoffs.
    mcc = b.get("mcclellan")
    if mcc is not None:
        if mcc <= -70:
            st, m = TURNED, f"McClellan at {mcc:.0f}. Extreme oversold breadth — broad selling, but the kind that can set up a reversal."
        elif mcc >= 70:
            st, m = COMPLACENT, f"McClellan at {mcc:+.0f}. Overbought breadth — the thrust is strong but stretched."
        else:
            st, m = STABLE, f"McClellan at {mcc:+.0f}. Breadth momentum is in a normal range."
        rows.append(_row("mcclellan", "macro", "McClellan Oscillator", mcc, "",
                         None, "", st, m))

    # 9 — HYG / SPY: high-yield credit vs equities. Is the bond market confirming
    #     the equity move, or quietly diverging (a risk-off tell)?
    spy = src.col(close, "SPY")
    hyg = src.col(close, "HYG")
    if spy is not None and hyg is not None:
        rel = (hyg / spy).dropna()
        if len(rel) > 21:
            hyg20, spy20 = src.pct_change_n(hyg, 20), src.pct_change_n(spy, 20)
            d20 = src.pct_change_n(rel, 20)
            if spy20 is not None and hyg20 is not None and spy20 > 0 and hyg20 < 0:
                st, m = WATCH, "Stocks are climbing but high-yield bonds are slipping — credit isn't confirming the rally, a quiet caution flag."
            elif spy20 is not None and hyg20 is not None and spy20 < 0 and hyg20 < 0:
                st, m = WATCH, "Both stocks and high-yield credit are falling together — broad risk-off."
            else:
                st, m = STABLE, "High-yield credit is keeping pace with stocks — risk appetite is intact and credit is confirming the move."
            rows.append(_row("hyg_spy", "macro", "High-Yield Credit (HYG/SPY)",
                             float(rel.iloc[-1]), "", d20, "%", st, m))

    # 10 — US dollar (DXY). A strengthening dollar tightens global financial
    #      conditions — a headwind for risk assets and commodities (disinflationary).
    dxy = src.col_any(close, "DX-Y.NYB", "UUP")
    if dxy is not None:
        z = src.zscore_mom(dxy, win=20); d20 = src.pct_change_n(dxy, 20)
        if (z is not None and z > 1.0) or (d20 is not None and d20 > 2.5):
            st, m = WATCH, "The dollar is strengthening — global financial conditions are tightening, a headwind for risk assets and commodities."
        elif (z is not None and z < -1.0) or (d20 is not None and d20 < -2.5):
            st, m = STABLE, "The dollar is weakening — easier financial conditions, a tailwind for risk assets, commodities and liquidity."
        else:
            st, m = STABLE, "The dollar is steady. Neutral for financial conditions."
        rows.append(_row("dxy", "macro", "US Dollar (DXY)", float(dxy.iloc[-1]), "",
                         d20, "%", st, m))

    # 11 — Bitcoin as a liquidity gauge. Rising BTC = global liquidity / risk
    #      appetite expanding; a sharp drop = liquidity draining out of markets.
    btc = src.col(close, "BTC-USD")
    if btc is not None:
        z = src.zscore_mom(btc, win=20); d20 = src.pct_change_n(btc, 20)
        if (z is not None and z < -1.0) or (d20 is not None and d20 < -12):
            st, m = WATCH, "Bitcoin is falling hard — a sign that global liquidity and risk appetite are draining out of markets."
        elif (z is not None and z > 0.8) or (d20 is not None and d20 > 12):
            st, m = STABLE, "Bitcoin is running — global liquidity and risk appetite are expanding, a tailwind for risk assets."
        else:
            st, m = STABLE, "Bitcoin is steady — liquidity conditions are neutral."
        rows.append(_row("bitcoin", "macro", "Bitcoin (liquidity)", float(btc.iloc[-1]), "",
                         d20, "%", st, m))

    return rows


def _panel_summary(rows, kind):
    """The roll-up line under each panel ('signs of complacency across N…')."""
    caution = [r for r in rows if r["state"] in _CAUTION]
    n = len(caution)
    if not rows:
        return "No data yet — run a breadth backfill and check the FRED key."
    if n == 0:
        return f"All {len(rows)} {kind} indicators look healthy. Conditions are constructive."
    comp = sum(1 for r in caution if r["state"] == COMPLACENT)
    if comp >= 2 and comp >= n - 1:
        return (f"Markets are showing signs of complacency across {comp} indicators. "
                "Conditions look calm, but historically this is when corrections start. Stay alert.")
    return (f"A mixed picture — {len(rows) - n} healthy but {n} flagging caution. "
            "The backdrop is transitioning; don't be complacent.")


# ---------------------------------------------------------------------------
# Public: panels + the regime feature blend
# ---------------------------------------------------------------------------

def build_indicators(raw):
    leading, macro = _leading(raw), _macro(raw)
    return {
        "leading": leading,
        "macro":   macro,
        "leading_summary": _panel_summary(leading, "leading"),
        "macro_summary":   _panel_summary(macro, "macro"),
        # Insider buy/sell: no clean free feed — deferred (the project's defer-with-
        # a-note pattern), so we don't render a dead row.
        "deferred": ["Insider Buy/Sell — no free SEC Form-4 feed wired yet"],
    }


def _clamp(z, lo=-3.0, hi=3.0):
    return None if z is None else float(max(lo, min(hi, z)))


def regime_features(raw):
    """The growth & inflation feature z-scores `regime.py` softmaxes into the
    4-quadrant probabilities, plus the per-input contributions (for the 'Driver:'
    line). Each input is a self-normalized z; a missing input drops out and the
    axis is the mean of whatever survived (fail-soft, never raises)."""
    close, fred = raw["close"], raw["fred"]
    b = (raw.get("breadth") or {}).get("metrics") or {}

    cg  = src.ratio(close, "HG=F", "GC=F")
    if cg is None:
        cg = src.ratio(close, "CPER", "GLD")
    copper = src.col_any(close, "HG=F", "CPER")
    gold   = src.col_any(close, "GC=F", "GLD")
    cyc    = src.basket(close, ["XLE", "XLF", "XLI"])
    dfn    = src.basket(close, ["XLU", "XLP", "XLV"])
    cyc_rel = (cyc / dfn).dropna() if (cyc is not None and dfn is not None) else None
    iwm    = src.ratio(close, "IWM", "SPY")
    xle    = src.ratio(close, "XLE", "SPY")
    dxy    = src.col_any(close, "DX-Y.NYB", "UUP")
    btc    = src.col(close, "BTC-USD")

    # A strengthening dollar tightens financial conditions (growth↓) and is
    # disinflationary (inflation↓), so the SAME signed term (weaker dollar = +) feeds
    # both axes — the reflation↔disinflation diagonal the dollar actually drives.
    dxy_mom = src.zscore_mom(dxy, win=20)
    dollar_weaker = None if dxy_mom is None else _clamp(-dxy_mom)

    p200 = b.get("pct_above_200")
    breadth_z = None if p200 is None else _clamp((p200 - 55) / 15.0)
    claims = fred.get("ICSA")
    claims_z = None if claims is None else _clamp(src.zscore_mom(claims, win=4))
    hy_z   = _clamp(src.zscore_level(fred.get("BAMLH0A0HYM2")))
    be_z   = _clamp(src.zscore_level(fred.get("T10YIE")))

    growth = [
        ("Copper/gold",            _clamp(src.zscore_mom(cg, win=20))),
        ("Credit spreads (tight)", None if hy_z is None else -hy_z),
        ("Cyclicals vs defensives", _clamp(src.zscore_mom(cyc_rel, win=20))),
        ("Small-cap risk appetite", _clamp(src.zscore_mom(iwm, win=20))),
        ("Jobless claims (falling)", None if claims_z is None else -claims_z),
        ("Breadth participation",   breadth_z),
        ("Bitcoin liquidity",       _clamp(src.zscore_mom(btc, win=20))),
        ("US dollar (weaker)",      dollar_weaker),
    ]
    inflation = [
        ("10Y breakeven",  be_z),
        ("Copper",         _clamp(src.zscore_mom(copper, win=20))),
        ("Energy leadership", _clamp(src.zscore_mom(xle, win=20))),
        ("Gold",           _clamp(src.zscore_mom(gold, win=20))),
        ("US dollar (weaker)", dollar_weaker),
    ]

    def _axis(items):
        vals = [(lbl, z) for lbl, z in items if z is not None]
        score = float(np.mean([z for _, z in vals])) if vals else 0.0
        return score, vals

    g_score, g_inputs = _axis(growth)
    i_score, i_inputs = _axis(inflation)
    return {
        "growth_z":      round(g_score, 3),
        "inflation_z":   round(i_score, 3),
        "growth_inputs": [[l, round(z, 2)] for l, z in g_inputs],
        "inflation_inputs": [[l, round(z, 2)] for l, z in i_inputs],
    }
