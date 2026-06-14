"""
Pump/dump alert heuristics — pure functions, unit-tested. DB writes and
notification dispatch happen in the callers (poller tick / EOD pass).

Kind semantics: `pump` = upward action starting (or washed-out bounce
potential), `dump` = downward action or exhaustion risk on a holding,
`info` = neither (earnings proximity, armed-screen matches). Extremes
(RSI, MA stretch) carry the kind of the REVERSAL they warn about.
"""

import math

from modules.screener.filters import apply_filters

RVOL_THRUST = 3.0    # rel volume needed for a thrust alert
CHG_THRUST  = 3.0    # |%change| needed for a thrust alert
RVOL_BUILD  = 2.0    # rel volume that flags volume building while price is quiet
RSI_HI      = 80.0
RSI_LO      = 20.0
MA_STRETCH  = 15.0   # |% from SMA20| flagged as overextended
GAP_PCT     = 4.0
EARNINGS_DAYS = 7


def _ok(v):
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def _fmt(v, nd=1):
    return f"{v:+.{nd}f}" if _ok(v) else "?"


def evaluate_rules(row):
    """row: snapshot dict (possibly live-patched) for one symbol.
    → [{rule_key, kind, message, detail}]"""
    out = []
    chg, rvol = row.get("chg_pct"), row.get("rvol_10d")
    px        = row.get("close")
    rsi       = row.get("rsi14")
    stretch   = row.get("price_vs_sma20_pct")
    gap       = row.get("gap_pct")
    dte       = row.get("days_to_earnings")

    def add(rule_key, kind, message, **detail):
        detail.setdefault("price", px)
        out.append({"rule_key": rule_key, "kind": kind,
                    "message": message, "detail": detail})

    # Volume + price thrust — the core pump/dump fingerprint
    if _ok(chg) and _ok(rvol) and rvol >= RVOL_THRUST:
        if chg >= CHG_THRUST:
            add("vol_thrust_up", "pump",
                f"{_fmt(chg)}% on {rvol:.1f}× volume — pump in progress",
                chg_pct=chg, rvol=rvol)
        elif chg <= -CHG_THRUST:
            add("vol_thrust_down", "dump",
                f"{_fmt(chg)}% on {rvol:.1f}× volume — dump in progress",
                chg_pct=chg, rvol=rvol)

    # Volume building — heavy tape but price still quiet: volume leads
    # price, so this is the pre-thrust read. |chg| < CHG_THRUST keeps it
    # mutually exclusive with the thrust alerts at any instant; distinct
    # rule keys let a name escalate building → thrust within the same day.
    if _ok(chg) and _ok(rvol) and rvol >= RVOL_BUILD and abs(chg) < CHG_THRUST:
        if chg >= 0:
            add("vol_building_up", "pump",
                f"{rvol:.1f}× volume, price quiet ({_fmt(chg)}%) — possible accumulation",
                chg_pct=chg, rvol=rvol)
        else:
            add("vol_building_down", "dump",
                f"{rvol:.1f}× volume, price slipping ({_fmt(chg)}%) — possible distribution",
                chg_pct=chg, rvol=rvol)

    # Technical extremes — exhaustion warnings, kind = reversal direction
    if _ok(rsi):
        if rsi >= RSI_HI:
            add("rsi_overbought", "dump",
                f"RSI {rsi:.0f} — overbought, reversal risk", rsi=rsi)
        elif rsi <= RSI_LO:
            add("rsi_oversold", "pump",
                f"RSI {rsi:.0f} — oversold, bounce potential", rsi=rsi)
    if _ok(stretch):
        if stretch >= MA_STRETCH:
            add("ma_stretch_up", "dump",
                f"{_fmt(stretch)}% above SMA20 — overextended", stretch=stretch)
        elif stretch <= -MA_STRETCH:
            add("ma_stretch_down", "pump",
                f"{_fmt(stretch)}% below SMA20 — washed out", stretch=stretch)

    # Breakout / breakdown levels (52w break suppresses the 20d echo)
    hi20, lo20 = row.get("high_20d"), row.get("low_20d")
    hi52, lo52 = row.get("high_252"), row.get("low_252")
    broke_52w_high = _ok(px) and _ok(hi52) and px > hi52
    broke_52w_low  = _ok(px) and _ok(lo52) and px < lo52
    if broke_52w_high:
        add("break_52w_high", "pump", f"new 52-week high through {hi52:.2f}",
            level=hi52)
    elif _ok(px) and _ok(hi20) and px > hi20:
        add("break_20d_high", "pump", f"breakout over 20-day high {hi20:.2f}",
            level=hi20)
    if broke_52w_low:
        add("break_52w_low", "dump", f"new 52-week low through {lo52:.2f}",
            level=lo52)
    elif _ok(px) and _ok(lo20) and px < lo20:
        add("break_20d_low", "dump", f"breakdown under 20-day low {lo20:.2f}",
            level=lo20)

    # Gaps
    if _ok(gap):
        if gap >= GAP_PCT:
            add("gap_up", "pump", f"gapped up {_fmt(gap)}%", gap_pct=gap)
        elif gap <= -GAP_PCT:
            add("gap_down", "dump", f"gapped down {_fmt(gap)}%", gap_pct=gap)

    # Earnings proximity
    if _ok(dte) and 0 <= dte <= EARNINGS_DAYS:
        add("earnings_soon", "info",
            f"earnings in {int(dte)} day{'s' if dte != 1 else ''}",
            days_to_earnings=dte)

    return out


def evaluate_armed_screens(live_df, focus_symbols, screens, prev_matches):
    """Fire an alert when a focus symbol NEWLY matches an armed screen.

    live_df: derived+patched scan frame indexed by symbol.
    prev_matches: {screen_id: set(symbols)} from the previous evaluation.
    → (alerts: [{symbol, rule_key, kind, message, detail}],
       new_state: {screen_id: set(symbols)})
    """
    alerts, new_state = [], {}
    focus = [s for s in focus_symbols if s in live_df.index]
    if not focus:
        return alerts, {s["id"]: set() for s in screens}
    sub = live_df.loc[focus]
    for screen in screens:
        matched = set(apply_filters(sub, screen["conditions"]).index)
        new_state[screen["id"]] = matched
        for sym in sorted(matched - prev_matches.get(screen["id"], set())):
            alerts.append({
                "symbol": sym,
                "rule_key": f"screen:{screen['id']}",
                "kind": "info",
                "message": f"newly matches screen “{screen['name']}”",
                "detail": {"screen": screen["name"]},
            })
    return alerts, new_state
