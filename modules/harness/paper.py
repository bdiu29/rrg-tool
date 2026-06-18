"""
Paper-trading engine (Phase 3 / Layer B) — trade the top watchlist suggestions.

The mechanism to clear the project's gate (Notes.txt step 6): paper-trade the harness's
combined read forward, COST-AWARE, and see whether the relative edge survives. Two books
run side-by-side, both cost-modeled:
  • long_only — buy the top tradeable picks; the rest in cash.
  • hedged    — the same longs, short SPY = the long gross (≈ market-neutral), so its
    return is the relative edge the referee measured. **The hedged book is the gate.**

Selection = the watchlist picks (`picks.suggest`). Sizing = the harness market read
(`combiner.combine`): the number of names by STANCE, gross exposure by POSTURE. Each
position carries the pick's 2×ATR STOP (the "if it goes south" exit). One idempotent
`step()` per trading day; a daily daemon (later) just calls it at the close.

REAL Schwab order placement is OUT OF SCOPE — this is paper only. Fills mark at the
step date's close (EOD convention). Prices are injectable so the engine tests offline.
"""

import json
import math
from datetime import date as _date

from modules.harness import store

STARTING_CAPITAL = 100_000.0
BOOKS            = ("long_only", "hedged")
HEDGE_SYM        = "SPY"

COST_BPS   = 5.0      # slippage on traded notional (bps) — the cost the gate must survive
COMMISSION = 0.0      # per-trade commission (ETFs/stocks ≈ free at most brokers)
DUST       = 1.0      # ignore orders below $1 notional

# Number of names by stance (concentrate when narrow, broaden when rotating).
MAX_NAMES = {"CONCENTRATE": 3, "ROTATE": 6, "NEUTRAL": 4}
# Gross long exposure by the combiner's posture (the market-timing layer).
GROSS_BY_POSTURE = {
    "Risk-on": 1.0, "Lean bullish": 0.75, "Neutral / mixed": 0.5,
    "Lean bearish": 0.25, "Risk-off": 0.0,
}


# ---------------------------------------------------------------------------
# Target portfolio (pure)
# ---------------------------------------------------------------------------

def target_book(suggestions, stance, posture):
    """Top tradeable picks → target long weights (∝ pick score, summing to the
    posture's gross). Pure. Returns {"long": {sym: weight}, "gross": float}."""
    tradeable = [s for s in (suggestions or []) if s.get("tradeable")]
    n = MAX_NAMES.get((stance or "").upper(), 4)
    gross = GROSS_BY_POSTURE.get(posture, 0.5)
    top = sorted(tradeable, key=lambda s: s.get("pick", 0), reverse=True)[:n]
    total = sum(max(0.0, s.get("pick", 0)) for s in top)
    if not top or gross <= 0 or total <= 0:
        return {"long": {}, "gross": 0.0}
    long_w = {s["symbol"]: gross * max(0.0, s["pick"]) / total for s in top}
    return {"long": long_w, "gross": gross, "stops": {s["symbol"]: s.get("stop") for s in top}}


# ---------------------------------------------------------------------------
# One book's daily rebalance
# ---------------------------------------------------------------------------

def _mark(cash, positions, prices):
    eq = cash
    for sym, p in positions.items():
        px = prices.get(sym)
        if px is not None:
            eq += p["shares"] * px
    return eq


def _step_book(book, date, decision, suggestions, prices):
    acct = store.get_account(book) or store.init_account(book, STARTING_CAPITAL, date)
    cash = acct["cash"]
    positions = store.get_positions(book)
    spy = prices.get(HEDGE_SYM)
    fills = []

    # 1. stop-outs first (the downside plan) — close ≤ stop forces a long exit.
    for sym in list(positions):
        p, px = positions[sym], prices.get(sym)
        if px is None or sym == HEDGE_SYM:
            continue
        if p["shares"] > 0 and p.get("stop") and px <= p["stop"]:
            cost = p["shares"] * px * COST_BPS / 1e4 + COMMISSION
            cash += p["shares"] * px - cost
            fills.append((sym, "SELL", p["shares"], px, p["shares"] * px, cost, "stop"))
            positions.pop(sym)

    equity = _mark(cash, positions, prices)        # size off the pre-rebalance mark

    # 2. target shares
    tgt = target_book(suggestions, decision.get("stance"), decision.get("posture"))
    target_shares, stops = {}, tgt.get("stops", {})
    for sym, w in tgt["long"].items():
        px = prices.get(sym)
        if px and px > 0:
            target_shares[sym] = (w * equity) / px
    if book == "hedged" and spy and spy > 0 and tgt["gross"] > 0:
        target_shares[HEDGE_SYM] = -(tgt["gross"] * equity) / spy

    # 3. orders = target − current
    turnover_notional = 0.0
    for sym in set(target_shares) | set(positions):
        px = prices.get(sym)
        if px is None:
            continue
        cur = positions.get(sym, {}).get("shares", 0.0)
        d = target_shares.get(sym, 0.0) - cur
        if abs(d * px) < DUST:
            continue
        notional = abs(d * px)
        cost = notional * COST_BPS / 1e4 + COMMISSION
        cash -= d * px + cost
        turnover_notional += notional
        fills.append((sym, "BUY" if d > 0 else "SELL", abs(d), px, notional, cost,
                      "rebalance"))
        new_sh = cur + d
        if abs(new_sh) < 1e-9:
            positions.pop(sym, None)
        else:
            prev = positions.get(sym, {})
            avg = prev.get("avg_cost") or px
            if (cur >= 0 and d > 0) or (cur <= 0 and d < 0):     # adding → weighted avg
                avg = (abs(cur) * (prev.get("avg_cost") or px) + abs(d) * px) / (abs(cur) + abs(d))
            stop = None if sym == HEDGE_SYM else stops.get(sym, prev.get("stop"))
            positions[sym] = {"shares": new_sh, "avg_cost": avg, "stop": stop}

    # 4. persist + record the step
    equity = _mark(cash, positions, prices)
    gross_val = sum(abs(p["shares"]) * prices.get(s, 0) for s, p in positions.items())
    net_val   = sum(p["shares"] * prices.get(s, 0) for s, p in positions.items())
    n_long    = sum(1 for s, p in positions.items() if p["shares"] > 0 and s != HEDGE_SYM)
    store.update_cash(book, cash)
    store.replace_positions(book, positions)
    store.add_fills(date, book, fills)
    store.record_step({
        "date": date, "book": book, "equity": round(equity, 2),
        "gross": round(gross_val, 2), "net": round(net_val, 2), "cash": round(cash, 2),
        "turnover": round(turnover_notional / equity * 100, 2) if equity else 0.0,
        "cost_paid": round(sum(f[5] for f in fills), 2), "n_long": n_long,
        "score": decision.get("score"), "stance": decision.get("stance"),
        "spy": spy, "rsp": prices.get("RSP"),
    })
    return {"book": book, "equity": round(equity, 2), "n_long": n_long,
            "fills": len(fills), "cost": round(sum(f[5] for f in fills), 2)}


# ---------------------------------------------------------------------------
# Live inputs (decision + suggestions + prices) — all fail-soft / injectable
# ---------------------------------------------------------------------------

def _live_decision():
    """The combiner's combined decision. Reuse today's cached brief decision when present
    (fast, consistent with the page, and NEVER triggers the LLM) — else a fresh
    deterministic compute via gather_all+combine (no LLM, just network-bound)."""
    from datetime import date
    try:
        from modules.harness import _MEM, _load_file
        today = date.today().isoformat()
        cached = (_MEM.get("payload") if _MEM.get("date") == today else None) or _load_file(today)
        if cached and cached.get("combined"):
            return cached["combined"]
    except Exception:
        pass
    from modules.harness import votes as votes_mod, combiner
    votes, regime, rotation = votes_mod.gather_all()
    return combiner.combine(votes, regime, rotation)


def _fetch_prices(symbols):
    from modules.rrg import signal
    syms = sorted(set(symbols) | {"SPY", "RSP"})
    close = signal._fetch_close(syms, "1d")
    out = {}
    for s in syms:
        if s in close.columns:
            col = close[s].dropna()
            if len(col):
                out[s] = float(col.iloc[-1])
    return out


def catch_up(today=None):
    """Mark-to-market catch-up for missed TRADING days. Between each book's last step and
    today, mark the HELD positions at each missed day's ACTUAL close (point-in-time,
    ffilled, no re-selection, no lookahead) and record one equity point per day — no
    fills, no cash change, no rebalance (today's rebalance is step()'s job). This keeps the
    equity curve dense + the drawdown honest through gaps when the app is closed for days.
    Trading days come from the price panel itself (so weekends/holidays are skipped for
    free). Fail-soft → 0. Returns the number of (date, book) rows backfilled."""
    from modules.rrg import signal
    today = today or _date.today().isoformat()

    held, last_info = set(), {}
    for book in BOOKS:
        steps = store.get_steps(book)
        if steps:
            last_info[book] = steps[-1]
            held |= set(store.get_positions(book))
    if not last_info:
        return 0                                    # nothing stepped yet → no gap to fill

    syms = sorted(held | {HEDGE_SYM, "RSP"})
    try:
        close = signal._fetch_close(syms, "1d")     # ffilled, Timestamp index, cached
    except Exception:
        return 0
    if close is None or getattr(close, "empty", True):
        return 0
    panel = {ts.strftime("%Y-%m-%d"): ts for ts in close.index}
    panel_dates = sorted(panel)

    def px(ts, s):
        try:
            v = float(close.at[ts, s])
        except Exception:
            return None
        return None if math.isnan(v) else v

    n = 0
    for book, last in last_info.items():
        last_date = last["date"]
        if last_date >= today:
            continue
        cash = (store.get_account(book) or {}).get("cash", 0.0)
        positions = store.get_positions(book)
        for d in panel_dates:
            if not (last_date < d < today) or store.step_exists(d, book):
                continue                            # only the gap, never re-backfill
            ts = panel[d]
            equity, gross, net = cash, 0.0, 0.0
            for s, p in positions.items():
                pr = px(ts, s)
                if pr is None:
                    continue
                equity += p["shares"] * pr
                gross += abs(p["shares"]) * pr
                net += p["shares"] * pr
            store.record_step({
                "date": d, "book": book, "equity": round(equity, 2),
                "gross": round(gross, 2), "net": round(net, 2), "cash": round(cash, 2),
                "turnover": 0.0, "cost_paid": 0.0,
                "n_long": sum(1 for s, p in positions.items()
                              if p["shares"] > 0 and s != HEDGE_SYM),
                "score": last.get("score"), "stance": last.get("stance"),
                "spy": px(ts, HEDGE_SYM), "rsp": px(ts, "RSP"),
            })
            n += 1
    return n


def step(asof=None, prices=None, decision=None, suggestions=None, force=False):
    """Advance ONE trading day for both books. Idempotent by default (a second call for
    the same date is a no-op) — the autonomous daemon relies on that. `force=True` (a
    user's explicit 'Step today') re-runs today, overwriting the day's step + re-marking;
    safe because re-stepping with unchanged data nets ~no trades (the dust filter)."""
    store.init_db()
    date = asof or _date.today().isoformat()
    # live path (no injected inputs) → first backfill any missed trading days, then step
    if asof is None and prices is None and decision is None and suggestions is None:
        try:
            catch_up(today=date)
        except Exception:
            pass
    pending = [b for b in BOOKS if force or not store.step_exists(date, b)]
    if not pending:
        return {"date": date, "skipped": True, "reason": "already stepped today",
                "books": []}

    if decision is None:
        decision = _live_decision()
    if suggestions is None:
        from modules.harness import picks
        suggestions = picks.suggest().get("suggestions", [])
    if prices is None:
        syms = {s["symbol"] for s in suggestions}
        for b in BOOKS:
            syms |= set(store.get_positions(b))
        prices = _fetch_prices(syms)

    store.record_decision(date, decision.get("stance"), decision.get("score"),
                          decision.get("posture"),
                          json.dumps([s["symbol"] for s in suggestions
                                      if s.get("tradeable")][:10]))
    results = [_step_book(b, date, decision, suggestions, prices) for b in pending]
    return {"date": date, "skipped": False, "stance": decision.get("stance"),
            "posture": decision.get("posture"), "books": results}


# ---------------------------------------------------------------------------
# State / reporting
# ---------------------------------------------------------------------------

def _maxdd(equity):
    peak, dd = -1e18, 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            dd = min(dd, e / peak - 1)
    return round(dd * 100, 2)


def _bench_return(steps, key):
    vals = [s[key] for s in steps if s.get(key)]
    if len(vals) < 2 or not vals[0]:
        return None
    return round((vals[-1] / vals[0] - 1) * 100, 2)


def _book_state(book):
    acct = store.get_account(book)
    steps = store.get_steps(book)
    positions = store.get_positions(book)
    if not acct or not steps:
        return {"inception": acct["inception_date"] if acct else None, "days": 0,
                "positions": [], "note": "not started — run a step"}
    eq = [s["equity"] for s in steps if s.get("equity") is not None]
    start = acct["start_equity"] or STARTING_CAPITAL
    total_ret = round((eq[-1] / start - 1) * 100, 2) if eq else None
    spy_ret, rsp_ret = _bench_return(steps, "spy"), _bench_return(steps, "rsp")
    return {
        "inception": acct["inception_date"], "days": len(steps),
        "equity": round(eq[-1], 2) if eq else None, "start_equity": start,
        "total_return": total_ret, "max_drawdown": _maxdd(eq) if eq else None,
        "spy_return": spy_ret, "rsp_return": rsp_ret,
        "vs_spy": round(total_ret - spy_ret, 2) if (total_ret is not None and spy_ret is not None) else None,
        "vs_rsp": round(total_ret - rsp_ret, 2) if (total_ret is not None and rsp_ret is not None) else None,
        "cost_paid": round(sum(s.get("cost_paid") or 0 for s in steps), 2),
        "avg_turnover": round(sum(s.get("turnover") or 0 for s in steps) / len(steps), 2),
        "last_step": steps[-1]["date"], "cash": round(acct["cash"], 2),
        "positions": sorted(
            [{"symbol": s, "shares": round(p["shares"], 2), "avg_cost": p.get("avg_cost"),
              "stop": p.get("stop")} for s, p in positions.items()],
            key=lambda x: x["symbol"]),
    }


def state():
    out = {"starting_capital": STARTING_CAPITAL,
           "books": {b: _book_state(b) for b in BOOKS}}
    hedged = out["books"].get("hedged", {})
    out["gate"] = {
        "metric": "hedged book cost-adjusted return vs SPY",
        "hedged_total_return": hedged.get("total_return"),
        "hedged_vs_spy": hedged.get("vs_spy"),
        "cost_paid": hedged.get("cost_paid"),
        "days": hedged.get("days", 0),
        "note": ("The hedged (long − SPY) book is the gate: its cost-adjusted return is "
                 "the relative edge surviving costs. Paper only — real money stays off "
                 "until this is clearly positive over a meaningful sample."),
    }
    return out
