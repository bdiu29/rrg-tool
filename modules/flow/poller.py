"""
Intraday options-chain poller — the singleton daemon that drives the flow feed.

Each tick (during the regular session) it sweeps the watch universe, pulls a Schwab
chain snapshot per underlying, diffs each contract against the prior poll for volume
deltas + clusters, scores it through the trader's 6 rules (`scoring`), persists the
flagged ones, and dispatches notable+ alerts. The first tick of a new day also runs
the OI-confirmation pass that resolves yesterday's signals into entered/exited.

Lifecycle mirrors the screener poller (idempotent start/stop, market-hours gate,
status dict). The same `run_pass()` is callable on demand (the /sync route).
"""

import threading
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from modules.flow import context, notify, scoring, source, store, universe

TICK_SECONDS = 90        # default; overridable via the `interval` setting
IDLE_SECONDS = 60
BURST_NOTIONAL = 100_000   # a poll's new volume worth ≥ this premium = a burst (cluster tick)
OI_CONFIRM_MIN = 500       # min |ΔOI| (contracts) to call an entry/exit confirmed

_ET = ZoneInfo("America/New_York")

_thread = None
_stop = threading.Event()
_lock = threading.Lock()
_status = {
    "running": False, "market_open": False, "last_tick": None, "last_error": None,
    "universe_count": 0, "signals_last": 0, "source": None, "note": None,
    "tier": None, "aggressor": None,
}


def market_open(now_et=None):
    now = now_et or datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def status():
    with _lock:
        snap = dict(_status)
    snap["market_open"] = market_open()
    return snap


def _focus_list():
    """Schwab positions ∪ screener watchlists, fail-soft to []."""
    syms = set()
    try:
        from modules.schwab import get_position_symbols
        syms |= set(get_position_symbols())
    except Exception:
        pass
    try:
        from modules.screener import store as scr_store
        syms |= set(scr_store.all_watchlist_symbols())
    except Exception:
        pass
    return sorted(syms)


def _alert_message(c, r):
    arrow = "calls" if c.get("put_call") == "CALL" else "puts"
    return (f"{c['underlying']} {c.get('strike')}{('C' if c.get('put_call')=='CALL' else 'P')} "
            f"{c.get('expiry')} — {r['classification'].upper()} {arrow} "
            f"(VOL/OI {r.get('vol_oi_ratio')}×, ${int((r.get('notional') or 0)/1000)}k, "
            f"conv {r.get('conviction')})")


# ---------------------------------------------------------------------------
# OI-confirmation pass (entered vs exited) — runs once per new day
# ---------------------------------------------------------------------------

def confirm_entries(today):
    """Resolve prior-day signals into opened/closed/flat from the OI delta now that
    today's open interest has printed. Needs today's OI already recorded (it is, for
    any contract still in the universe and polled this session)."""
    for sig in store.unconfirmed_before(today):
        today_oi = store.oi_on(sig["option_symbol"], today)
        if today_oi is None:
            continue
        base = sig.get("open_interest") or 0
        delta = today_oi - base
        thr = max(OI_CONFIRM_MIN, 0.2 * base)
        if delta >= thr:
            ee = "opened"
        elif delta <= -thr:
            ee = "closed"
        else:
            ee = "flat"
        store.set_entry_exit(sig["date"], sig["option_symbol"], ee)


# ---------------------------------------------------------------------------
# One scan pass (shared by the tick and the /sync route)
# ---------------------------------------------------------------------------

def run_pass():
    """One full sweep of the universe → persisted signals + dispatched alerts.
    Returns the count of contracts that scored above noise this pass."""
    src, note = source.resolve_source(store.get_setting("source", "schwab"))
    caps = src.capabilities()
    universe_syms = universe.merge_universe(_focus_list(), store.get_setting("universe"))
    burst = store.get_setting("burst_notional", BURST_NOTIONAL)
    now = datetime.now(_ET)
    day = now.strftime("%Y-%m-%d")

    with _lock:
        _status.update(universe_count=len(universe_syms), source=caps["source"],
                       tier=caps["tier"], aggressor=caps["aggressor"], note=note)

    confirm_entries(day)                       # resolve yesterday's signals first
    ctx_map = context.build_context(universe_syms)

    new_alerts = []
    ticker_notional = defaultdict(float)
    flagged = 0

    for sym in universe_syms:
        try:
            chain = src.get_chain(sym)
        except Exception as e:
            with _lock:
                _status["last_error"] = f"{sym}: {e}"
            continue
        base = store.ticker_baseline(sym, day)
        for c in chain:
            if not c.get("option_symbol"):
                continue
            mark = c.get("mark") or c.get("last") or 0
            ticker_notional[sym] += (c.get("session_volume") or 0) * mark * 100

            vol_delta, cluster = store.record_poll(c, day, burst)
            c["volume_delta"] = vol_delta
            c["cluster_count"] = cluster
            c["baseline_notional"] = base
            c["confluence"] = ctx_map.get(sym)

            r = scoring.classify_contract(c)
            store.record_oi(c["option_symbol"], day, c.get("open_interest"))
            if r["classification"] == "noise":
                continue
            flagged += 1
            store.upsert_flow_signal(day, c, r, c.get("confluence"))

            if r["classification"] in ("notable", "conviction"):
                kind = "bull" if r["direction"] == "bullish" else "bear"
                aid = store.insert_alert(
                    day, c["option_symbol"], f"flow:{r['classification']}", kind,
                    _alert_message(c, r),
                    {"conviction": r["conviction"], "vol_oi": r.get("vol_oi_ratio"),
                     "notional": r.get("notional"), "factors": r.get("factors")})
                if aid:
                    new_alerts.append({"id": aid, "kind": kind,
                                       "message": _alert_message(c, r)})

    for sym, notional in ticker_notional.items():
        store.record_ticker_notional(sym, day, notional)

    notify.dispatch(new_alerts)
    with _lock:
        _status.update(last_tick=now.strftime("%Y-%m-%d %H:%M:%S"),
                       signals_last=flagged, last_error=None)
    return flagged


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------

def _interval():
    try:
        return int(store.get_setting("interval", TICK_SECONDS))
    except (TypeError, ValueError):
        return TICK_SECONDS


def _loop():
    while not _stop.is_set():
        if market_open():
            try:
                run_pass()
            except Exception as e:
                with _lock:
                    _status["last_error"] = str(e)
            _stop.wait(_interval())
        else:
            _stop.wait(IDLE_SECONDS)
    with _lock:
        _status["running"] = False


def start():
    """Idempotent; safe at every boot."""
    global _thread
    with _lock:
        if _status["running"]:
            return False
        _status["running"] = True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="flow-poller")
    _thread.start()
    return True


def stop():
    _stop.set()
    with _lock:
        _status["running"] = False
