"""
Intraday quote poller + the shared scan-frame/alert-pass machinery.

A singleton daemon thread ticks every TICK_SECONDS during the regular session
(9:30–16:00 ET, Mon–Fri): it pulls rich Schwab quotes for the focus list
(positions ∪ watchlists), patches those snapshot rows with live values, runs
the pump/dump rules and armed screens, and dispatches newly inserted alerts.
Outside the session it idles. Holidays aren't modeled — polling an empty
session is harmless.

build_scan_frame()/run_alert_pass() also serve the scan route and the EOD
pass in snapshot.py, so live ticks and end-of-day evaluation share one path.
"""

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from modules.screener import metrics, notify, quotes, rules, store
from modules.screener.filters import derive_scan_columns

TICK_SECONDS   = 180
IDLE_SECONDS   = 60
POS_CACHE_SECS = 600

_ET = ZoneInfo("America/New_York")

_thread = None
_stop   = threading.Event()
_lock   = threading.Lock()

_status = {
    "running":     False,
    "market_open": False,
    "last_tick":   None,
    "focus_count": 0,
    "last_error":  None,
    "interval":    TICK_SECONDS,
}

_live_lock = threading.Lock()
_live      = {"rows": {}, "ts": None}

# ts None = never fetched — monotonic() can start near 0 on macOS, so a 0.0
# sentinel would read as "fresh" for the first 10 minutes of the process
_pos_cache = {"symbols": [], "ts": None}


def market_open(now_et=None):
    now = now_et or datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def get_live():
    """{symbol: {close, chg_pct, volume, rvol_10d, gap_pct}}, plus timestamp."""
    with _live_lock:
        return dict(_live["rows"]), _live["ts"]


def status():
    with _lock:
        snap = dict(_status)
    snap["market_open"] = market_open()
    return snap


def position_symbols():
    """Schwab position symbols, cached, fail-soft to [] when unauthenticated."""
    now = time.monotonic()
    if _pos_cache["ts"] is not None and now - _pos_cache["ts"] < POS_CACHE_SECS:
        return list(_pos_cache["symbols"])
    try:
        from modules.schwab import get_position_symbols
        syms = get_position_symbols()
    except Exception:
        syms = list(_pos_cache["symbols"])   # keep last known on hiccups
    _pos_cache.update(symbols=syms, ts=now)
    return list(syms)


def focus_list():
    return sorted(set(position_symbols()) | set(store.all_watchlist_symbols()))


# ---------------------------------------------------------------------------
# Scan frame — snapshot + fundamentals + sector RRG calls (+ live patch)
# ---------------------------------------------------------------------------

def _sector_rrg_calls():
    """{sector_etf: rotation call} — fail-soft to {} if rrg can't compute."""
    try:
        from modules.rrg import compute_rrg, BENCHMARK, DEFAULT_TICKERS
        rrg = compute_rrg(DEFAULT_TICKERS, BENCHMARK, "1d")
        return {etf: d["call"] for etf, d in rrg["sectors"].items()}
    except Exception:
        return {}


def build_scan_frame(live=True, include_rrg=True):
    """→ (DataFrame indexed by symbol, [symbols that got live patches])"""
    df = store.get_snapshot()
    if df.empty:
        return df, []
    fund = store.get_fundamentals()
    df   = df.join(fund, how="left")
    df["rrg_call"] = df["sector_etf"].map(_sector_rrg_calls()) if include_rrg else None

    patched = []
    if live:
        live_rows, _ts = get_live()
        for sym, vals in live_rows.items():
            if sym not in df.index:
                continue
            for col in ("close", "chg_pct", "volume", "rvol_10d", "gap_pct"):
                if vals.get(col) is not None:
                    df.loc[sym, col] = vals[col]
            patched.append(sym)
    return derive_scan_columns(df), patched


# ---------------------------------------------------------------------------
# Alert pass — shared by live ticks and the EOD pass
# ---------------------------------------------------------------------------

def run_alert_pass(df, focus, date):
    """Evaluate heuristics + armed screens for focus symbols over `df`,
    insert alerts (store dedupes), dispatch newly inserted ones."""
    new_alerts = []
    pos_syms   = set(_pos_cache["symbols"])

    for sym in focus:
        if sym not in df.index:
            continue
        row = df.loc[sym].to_dict()
        for hit in rules.evaluate_rules(row):
            aid = store.insert_alert(date, sym, hit["rule_key"], hit["kind"],
                                     hit["message"], hit["detail"])
            if aid:
                new_alerts.append({"id": aid, "symbol": sym, **hit})

    armed = store.list_screens(armed_only=True)
    if armed:
        prev = {s["id"]: store.get_screen_matches(s["id"]) for s in armed}
        screen_alerts, new_state = rules.evaluate_armed_screens(df, focus, armed, prev)
        for hit in screen_alerts:
            aid = store.insert_alert(date, hit["symbol"], hit["rule_key"],
                                     hit["kind"], hit["message"], hit["detail"])
            if aid:
                new_alerts.append({"id": aid, **hit})
        for sid, matched in new_state.items():
            store.update_screen_matches(sid, matched, date)

    notify.dispatch(new_alerts, position_symbols=pos_syms)
    return new_alerts


# ---------------------------------------------------------------------------
# Tick + thread lifecycle
# ---------------------------------------------------------------------------

def _tick():
    focus = focus_list()
    with _lock:
        _status["focus_count"] = len(focus)
    if not focus:
        return

    rich = quotes.get_rich_quotes(focus)
    snap = store.get_snapshot()
    frac = metrics.session_fraction(datetime.now(_ET))

    rows = {}
    for sym, q in rich.items():
        avg = None
        if sym in snap.index:
            avg = snap.loc[sym, "avg_vol_10d"]
            avg = None if pd.isna(avg) else float(avg)
        chg = q.get("net_pct_chg")
        if chg is None and q.get("prev_close"):
            chg = (q["last"] / q["prev_close"] - 1) * 100
        gap = None
        if q.get("open") and q.get("prev_close"):
            gap = (q["open"] / q["prev_close"] - 1) * 100
        rows[sym] = {
            "close":    q["last"],
            "chg_pct":  chg,
            "volume":   q.get("total_volume"),
            "rvol_10d": metrics.live_rvol(q.get("total_volume"), avg, frac),
            "gap_pct":  gap,
        }
    with _live_lock:
        _live["rows"] = rows
        _live["ts"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    df, _patched = build_scan_frame(live=True)
    run_alert_pass(df, focus, datetime.now(_ET).strftime("%Y-%m-%d"))
    with _lock:
        _status["last_tick"]  = _live["ts"]
        _status["last_error"] = None


def _loop():
    while not _stop.is_set():
        if market_open():
            try:
                _tick()
            except Exception as e:
                with _lock:
                    _status["last_error"] = str(e)
            _stop.wait(TICK_SECONDS)
        else:
            _stop.wait(IDLE_SECONDS)
    with _lock:
        _status["running"] = False


def start():
    """Idempotent; safe to call at every server boot."""
    global _thread
    with _lock:
        if _status["running"]:
            return False
        _status["running"] = True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="screener-poller")
    _thread.start()
    return True


def stop():
    _stop.set()
    with _lock:
        _status["running"] = False
