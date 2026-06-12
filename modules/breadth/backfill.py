"""
Resumable background sync: one-time historical backfill + daily incremental
append, designed around Schwab's 120/min limit and per-symbol history calls.

A single job runs at a time (daemon thread). Progress is queryable, the job
is stop-flag interruptible, and re-running it is the resume path — symbols
whose stored history is already current are skipped via sync_state, so an
interrupted 3,000-symbol pull just continues where it left off.
"""

import threading
import time
from datetime import datetime, timedelta

import pandas as pd

from . import indicators, store, universes
from .datasource import YFinanceDataSource, resolve_datasource

HISTORY_YEARS     = 3   # ≥252d warmup for 52-wk highs/200d MA, plus buffer
INCR_OVERLAP_DAYS = 7   # re-fetch overlap on incremental pulls (idempotent)
MAX_FAILURE_LOG   = 50

_lock = threading.Lock()
_stop = threading.Event()

_state = {
    "state":       "idle",   # idle | running | done | error
    "universe":    None,
    "source":      None,
    "total":       0,
    "done":        0,
    "skipped":     0,
    "failed":      0,
    "failures":    [],
    "message":     "",
    "note":        None,
    "started":     None,
    "finished":    None,
    "eta_seconds": None,
}


def get_progress():
    with _lock:
        snap = dict(_state)
        snap["failures"] = list(_state["failures"][:MAX_FAILURE_LOG])
        return snap


def request_stop():
    _stop.set()


def start_sync(universe_key, source_name=None):
    """Kick off a backfill/update for one universe. Returns (ok, message)."""
    with _lock:
        if _state["state"] == "running":
            return False, f"sync already running for '{_state['universe']}'"
        _state.update(
            state="running", universe=universe_key, source=source_name,
            total=0, done=0, skipped=0, failed=0, failures=[],
            message="starting…", note=None, eta_seconds=None,
            started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), finished=None,
        )
    _stop.clear()
    threading.Thread(target=_run, args=(universe_key, source_name), daemon=True).start()
    return True, "started"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _set(**kw):
    with _lock:
        _state.update(kw)


def _record_failure(symbol, err):
    with _lock:
        _state["failed"] += 1
        if len(_state["failures"]) < MAX_FAILURE_LOG:
            _state["failures"].append(f"{symbol}: {err}")


def _target_date():
    """Most recent date we expect a closed daily bar for (NY time; before the
    16:00 close, yesterday). Holidays aren't modeled — worst case is a small
    redundant re-fetch, which upserts make idempotent."""
    now = pd.Timestamp.now(tz="America/New_York")
    d   = now.normalize()
    if now.hour < 16:
        d -= pd.Timedelta(days=1)
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _full_start():
    return (datetime.now() - timedelta(days=int(HISTORY_YEARS * 365.25))).strftime("%Y-%m-%d")


def _sync_symbol(src, symbol, start, end, schwab_symbol=None):
    """Fetch + upsert one symbol. Index symbols get a per-adapter name and a
    yfinance fallback (Schwab index entitlement can differ by app)."""
    fetch_sym = symbol
    if schwab_symbol and src.name == "schwab":
        fetch_sym = schwab_symbol
    try:
        df = src.get_price_history(fetch_sym, start, end)
    except Exception:
        if symbol.startswith("^") or schwab_symbol:
            df = YFinanceDataSource().get_price_history(symbol, start, end)
        else:
            raise
    n = store.upsert_bars(symbol, df)
    if n:
        store.set_sync_state(symbol, df["date"].max())
    else:
        store.set_sync_state(symbol, None, status="failed", error="no data returned")
    return n


def _run(universe_key, source_name):
    try:
        cfg       = universes.load_config()
        preferred = source_name or cfg["settings"].get("datasource", "schwab")
        src, note = resolve_datasource(preferred)
        _set(source=src.name, note=note, message="refreshing constituents…")

        symbols = universes.get_constituents(universe_key)
        uni     = cfg["universes"][universe_key]
        target  = _target_date()
        end     = datetime.now().strftime("%Y-%m-%d")

        # Index + concentration-gauge series ride along with every sync.
        conc   = cfg.get("concentration", {})
        extras = [(uni["index"]["symbol"], uni["index"].get("schwab"))]
        for s in (conc.get("numerator"), conc.get("denominator")):
            if s and s != uni["index"]["symbol"]:
                extras.append((s, None))

        states  = store.get_sync_states(symbols)
        pending = []
        for sym in symbols:
            st = states.get(sym)
            if st and st.get("last_date") and st["last_date"] >= target:
                continue
            pending.append((sym, st.get("last_date") if st else None))
        skipped = len(symbols) - len(pending)
        _set(total=len(symbols) + len(extras), skipped=skipped, done=skipped,
             message=f"syncing {len(pending)} symbols ({skipped} already current)…")

        for sym, schwab_sym in extras:
            try:
                _sync_symbol(src, sym, _full_start() if not store.last_bar_date(sym)
                             else (datetime.strptime(store.last_bar_date(sym), "%Y-%m-%d")
                                   - timedelta(days=INCR_OVERLAP_DAYS)).strftime("%Y-%m-%d"),
                             end, schwab_symbol=schwab_sym)
            except Exception as e:
                _record_failure(sym, e)
            with _lock:
                _state["done"] += 1

        t0, processed = time.monotonic(), 0

        def tick(n=1):
            nonlocal processed
            processed += n
            with _lock:
                _state["done"] += n
                if processed:
                    rate = (time.monotonic() - t0) / processed
                    _state["eta_seconds"] = int(rate * (len(pending) - processed))

        if src.supports_bulk:
            # Bulk path: full-history group and incremental group, chunked
            # inside the adapter. Upserts keep overlap harmless.
            new_syms  = [s for s, last in pending if not last]
            incr      = [(s, last) for s, last in pending if last]
            if new_syms:
                _set(message=f"bulk backfill: {len(new_syms)} new symbols…")
                got = src.bulk_price_history(new_syms, _full_start(), end)
                for s in new_syms:
                    df = got.get(s)
                    if df is not None and not df.empty:
                        store.upsert_bars(s, df)
                        store.set_sync_state(s, df["date"].max())
                    else:
                        store.set_sync_state(s, None, status="failed", error="no data returned")
                        _record_failure(s, "no data returned")
                tick(len(new_syms))
            if incr and not _stop.is_set():
                start = (datetime.strptime(min(l for _, l in incr), "%Y-%m-%d")
                         - timedelta(days=INCR_OVERLAP_DAYS)).strftime("%Y-%m-%d")
                _set(message=f"bulk update: {len(incr)} symbols…")
                got = src.bulk_price_history([s for s, _ in incr], start, end)
                for s, _ in incr:
                    df = got.get(s)
                    if df is not None and not df.empty:
                        store.upsert_bars(s, df)
                        store.set_sync_state(s, df["date"].max())
                tick(len(incr))
        else:
            for sym, last in pending:
                if _stop.is_set():
                    _set(message="stopped — re-run sync to resume")
                    break
                start = (_full_start() if not last else
                         (datetime.strptime(last, "%Y-%m-%d")
                          - timedelta(days=INCR_OVERLAP_DAYS)).strftime("%Y-%m-%d"))
                try:
                    _sync_symbol(src, sym, start, end)
                except Exception as e:
                    store.set_sync_state(sym, last, status="failed", error=str(e))
                    _record_failure(sym, e)
                tick()

        _set(message="computing breadth series…", eta_seconds=None)
        n_days = indicators.compute_universe(universe_key)

        with _lock:
            failed = _state["failed"]
        msg = f"done — {n_days} breadth days computed"
        if failed:
            msg += f" ({failed} symbols failed)"
        _set(state="done", message=msg,
             finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        _set(state="error", message=str(e),
             finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
