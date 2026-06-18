"""
Autonomous paper daemon (Phase 4) — steps the paper book once per trading-day close.

Mirrors the flow/screener poller lifecycle (singleton thread, idempotent start/stop,
status dict), but instead of ticking through the session it fires ONCE after the close:
the pure `is_due` gate requires autonomous mode + a weekday + ≥ 16:05 ET (so the day's
close is published) + not already auto-stepped today. `paper.step()` is itself
idempotent (UNIQUE(date,book)), so a double-fire is harmless.

The daemon is started at boot but is a NO-OP in manual mode (the default), so it never
trades unless the user explicitly switches to autonomous. Paper only — it cannot place
real orders. (Market holidays are unmodeled: a step on a holiday just re-marks at the
prior close, a 0-return day — harmless to the gate.)
"""

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from modules.harness import store

TICK_SECONDS = 300          # check every 5 min; it only acts once/day
CLOSE_MIN    = 16 * 60 + 5  # 16:05 ET — after the close, so the day's bar is final
_ET = ZoneInfo("America/New_York")

_thread = None
_stop = threading.Event()
_lock = threading.Lock()
_status = {"running": False, "mode": "manual", "last_auto_step": None,
           "last_check": None, "last_error": None, "last_result": None}


def is_due(now_et, mode, last_step_date, today):
    """PURE: should the daemon auto-step right now? Autonomous + a weekday + at/after
    16:05 ET + not already auto-stepped today."""
    if (mode or "manual") != "autonomous":
        return False
    if now_et.weekday() >= 5:                       # Sat/Sun
        return False
    if now_et.hour * 60 + now_et.minute < CLOSE_MIN:
        return False
    return last_step_date != today


def _maybe_step():
    now = datetime.now(_ET)
    mode = store.get_setting("trading_mode", "manual")
    last = store.get_setting("last_auto_step")
    today = now.date().isoformat()
    with _lock:
        _status.update(mode=mode, last_auto_step=last,
                       last_check=now.isoformat(timespec="seconds"))
    if not is_due(now, mode, last, today):
        return
    from modules.harness import paper
    res = paper.step()                              # idempotent
    store.set_setting("last_auto_step", today)
    with _lock:
        _status["last_auto_step"] = today
        _status["last_result"] = {"date": res.get("date"), "skipped": res.get("skipped")}


def _loop():
    while not _stop.is_set():
        try:
            _maybe_step()
        except Exception as e:
            with _lock:
                _status["last_error"] = str(e)
        _stop.wait(TICK_SECONDS)
    with _lock:
        _status["running"] = False


def start():
    """Idempotent; safe at every boot. The loop is inert until the mode is autonomous."""
    global _thread
    with _lock:
        if _status["running"]:
            return False
        _status["running"] = True
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="paper-poller")
    _thread.start()
    return True


def stop():
    _stop.set()
    with _lock:
        _status["running"] = False


def status():
    with _lock:
        snap = dict(_status)
    snap["mode"] = store.get_setting("trading_mode", "manual")
    snap["last_auto_step"] = store.get_setting("last_auto_step")
    return snap
