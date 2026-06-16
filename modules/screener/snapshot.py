"""
Background jobs for the screener (singleton daemon thread, breadth-backfill
pattern): the daily snapshot rebuild and the fundamentals refresh.

Snapshot: union of all breadth universes → OHLCV panels from the breadth bars
store → metrics.compute_snapshot → wholesale table replace → one EOD alert
pass over the focus list (catches days the app wasn't running intraday).

Fundamentals: Schwab instruments first (full universe in minutes), yfinance
fills whatever Schwab left empty, then earnings dates for the focus list and
sector classification for the long tail (yfinance is the only source for
those two). Every phase is resumable via per-symbol status/updated_at.
"""

import threading
import time
from datetime import datetime, timedelta

import pandas as pd

from modules.breadth import store as breadth_store
from modules.breadth import universes as breadth_universes
from modules.breadth.datasource import resolve_datasource
from modules.confluence import flags as rrg_flags
from modules.screener import metrics, poller, quotes, store

YF_THROTTLE  = 0.5    # seconds between per-symbol yfinance calls
FUND_MAX_AGE = 7      # days before Schwab numbers are considered stale
MAX_FAILURE_LOG = 50

FLAGSTATS_MAX_AGE = 90     # days a symbol's flag win-rate is cached before recompute
FLAGSTATS_DAYS    = 1125   # ~3y of bars for a stable per-symbol win-rate
FLAGSTATS_CHUNK   = 500    # symbols per panel load (keeps the working set small)

_lock = threading.Lock()
_stop = threading.Event()

_state = {
    "state":    "idle",   # idle | running | done | error
    "kind":     None,     # snapshot | fundamentals | both
    "phase":    None,
    "total":    0,
    "done":     0,
    "failed":   0,
    "failures": [],
    "message":  "",
    "started":  None,
    "finished": None,
}


def get_progress():
    with _lock:
        snap = dict(_state)
        snap["failures"] = list(_state["failures"][:MAX_FAILURE_LOG])
        return snap


def request_stop():
    _stop.set()


def start_refresh(kind="snapshot"):
    """kind: snapshot | fundamentals | both | flagstats. → (ok, message)"""
    if kind not in ("snapshot", "fundamentals", "both", "flagstats"):
        return False, f"unknown refresh kind '{kind}'"
    with _lock:
        if _state["state"] == "running":
            return False, f"a {_state['kind']} refresh is already running"
        _state.update(
            state="running", kind=kind, phase=None, total=0, done=0,
            failed=0, failures=[], message="starting…",
            started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), finished=None,
        )
    _stop.clear()
    threading.Thread(target=_run, args=(kind,), daemon=True,
                     name="screener-refresh").start()
    return True, "started"


def _set(**kw):
    with _lock:
        _state.update(kw)


def _fail(symbol, err):
    with _lock:
        _state["failed"] += 1
        if len(_state["failures"]) < MAX_FAILURE_LOG:
            _state["failures"].append(f"{symbol}: {err}")


def bars_date():
    """Newest bar date in the breadth store (None if no bars yet)."""
    with breadth_store.connect() as conn:
        row = conn.execute("SELECT MAX(date) FROM bars").fetchone()
    return row[0] if row and row[0] else None


def needs_refresh():
    latest = bars_date()
    snap   = store.get_meta("snapshot_date")
    return bool(latest) and (snap is None or snap < latest)


def needs_flagstats_refresh():
    """Kick the (incremental) flag win-rate job at most once a calendar day; the
    job itself recomputes only symbols older than FLAGSTATS_MAX_AGE, so a daily
    kick on a fresh table is nearly free."""
    today = datetime.now().strftime("%Y-%m-%d")
    return store.get_meta("flagstats_date") != today


def _universe_symbols():
    cfg  = breadth_universes.load_config()
    syms = set()
    for key in cfg["universes"]:
        syms.update(breadth_store.get_members(key))
    return sorted(syms)


# ---------------------------------------------------------------------------
# Snapshot rebuild
# ---------------------------------------------------------------------------

def _spy_series(start):
    """SPY closes for the RS columns: bars store if present, else one
    datasource fetch (Schwab → yfinance fallback)."""
    df = breadth_store.get_series("SPY", start=start)
    if df.empty:
        src, _note = resolve_datasource("schwab")
        raw = src.get_price_history("SPY", start,
                                    datetime.now().strftime("%Y-%m-%d"))
        breadth_store.upsert_bars("SPY", raw)   # cache for next rebuild
        df = raw.set_index("date")[["close"]]
    return df["close"]


def _run_snapshot():
    symbols = _universe_symbols()
    if not symbols:
        _set(message="no universe members synced yet — run a breadth sync first")
        return
    start = (datetime.now()
             - timedelta(days=metrics.SNAPSHOT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    _set(phase="snapshot", message=f"loading panels for {len(symbols)} symbols…",
         total=4, done=0)
    close, volume, open_, high, low = breadth_store.get_panels(
        symbols, start=start, fields=("close", "volume", "open", "high", "low"))
    _set(done=1, message="fetching SPY for relative strength…")
    spy = _spy_series(start)

    _set(done=2, message="computing indicators…")
    snap = metrics.compute_snapshot(close, volume, open_, high, low, spy)

    _set(done=3, message="writing snapshot…")
    n = store.replace_snapshot(snap)
    snap_date = close.index.max() if not close.empty else None
    if snap_date:
        store.set_meta("snapshot_date", snap_date)

    # EOD alert pass — same path as a live tick, minus the live patch.
    focus = poller.focus_list()
    if focus and snap_date:
        df, _patched = poller.build_scan_frame(live=False)
        poller.run_alert_pass(df, focus, snap_date)
    _set(done=4, message=f"snapshot done — {n} symbols as of {snap_date}")


# ---------------------------------------------------------------------------
# Flag win-rate precompute (incremental, chunked, ~90-day cached per symbol)
# ---------------------------------------------------------------------------

def _regime_labels():
    """Market regime label Series (HEALTHY/NEUTRAL/DETERIORATING) over history,
    from the breadth module. Used to condition the flag win-rates. Fail-soft."""
    try:
        from modules.breadth import _full_series
        from modules.breadth import regime as breadth_regime
        agg, der, _index = _full_series("sp500")
        if agg is None:
            return None
        return breadth_regime.regime_series(der["summation"], agg["pct_above_200"])
    except Exception:
        return None


def _run_flagstats():
    symbols = _universe_symbols()
    if not symbols:
        _set(message="no universe members synced yet — run a breadth sync first")
        return
    stale = store.stale_winrate_symbols(symbols, FLAGSTATS_MAX_AGE)
    if not stale:
        store.set_meta("flagstats_date", datetime.now().strftime("%Y-%m-%d"))
        _set(phase="flagstats", total=0, done=0,
             message="flag win-rates fresh — nothing to recompute")
        return

    regime = _regime_labels()
    conditioned = regime is not None
    start = (datetime.now() - timedelta(days=FLAGSTATS_DAYS)).strftime("%Y-%m-%d")
    _set(phase="flagstats", total=len(stale), done=0,
         message=f"flag win-rates: {len(stale)} symbols "
                 f"({'regime-conditioned' if conditioned else 'unconditioned'})…")

    for i in range(0, len(stale), FLAGSTATS_CHUNK):
        if _stop.is_set():
            return
        chunk = stale[i:i + FLAGSTATS_CHUNK]
        close, volume = breadth_store.get_panels(chunk, start=start,
                                                 fields=("close", "volume"))
        if conditioned and not close.empty:
            from modules.breadth import regime as breadth_regime
            reg_arr = breadth_regime.align_labels(regime, close.index).to_numpy()
        else:
            reg_arr = None
        for s in chunk:
            if _stop.is_set():
                return
            try:
                if close.empty or s not in close.columns:
                    wr = {"bull": None, "bull_n": 0, "bear": None, "bear_n": 0}
                else:
                    c = close[s].to_numpy(dtype=float)
                    v = (volume[s].to_numpy(dtype=float)
                         if s in volume.columns else None)
                    wr = rrg_flags.win_rates(c, v, regime_labels=reg_arr)
                store.upsert_flag_winrate(s, wr["bull"], wr["bull_n"],
                                          wr["bear"], wr["bear_n"], conditioned)
            except Exception as e:
                _fail(s, e)
            _tick()
        del close, volume      # release the chunk's panels before the next load

    store.set_meta("flagstats_date", datetime.now().strftime("%Y-%m-%d"))
    _set(message=f"flag win-rates done — {len(stale)} symbols recomputed")


# ---------------------------------------------------------------------------
# Fundamentals refresh
# ---------------------------------------------------------------------------

def _phase(name, items, message):
    _set(phase=name, total=len(items), done=0, message=message)


def _tick():
    with _lock:
        _state["done"] += 1


def _schwab_available():
    try:
        from modules.schwab import get_access_token
        get_access_token()
        return True
    except Exception:
        return False


def _run_fundamentals():
    symbols = _universe_symbols()
    focus   = poller.focus_list()
    # focus names should always have fundamentals, even off-universe ones
    symbols = sorted(set(symbols) | set(focus))
    if not symbols:
        _set(message="no symbols to refresh")
        return

    # Phase 1 — Schwab instruments, batched (the fast bulk path)
    if _schwab_available():
        stale = store.stale_fundamental_symbols(symbols, FUND_MAX_AGE)
        _phase("schwab numbers", stale, f"Schwab fundamentals: {len(stale)} symbols…")
        for i in range(0, len(stale), quotes.FUND_BATCH):
            if _stop.is_set():
                return
            chunk = stale[i:i + quotes.FUND_BATCH]
            try:
                got = quotes.get_schwab_fundamentals(chunk)
            except Exception as e:
                _fail(f"batch {chunk[0]}…", e)
                continue
            for sym in chunk:
                fields = got.get(sym)
                if fields:
                    store.upsert_fundamental(sym, status="schwab", **fields)
            with _lock:
                _state["done"] += len(chunk)

    # Phase 2 — yfinance gap-fill for anything still missing market cap
    import yfinance as yf
    missing = store.stale_fundamental_symbols(symbols, FUND_MAX_AGE,
                                              column="market_cap")
    _phase("yfinance gap-fill", missing, f"gap-fill: {len(missing)} symbols…")
    for sym in missing:
        if _stop.is_set():
            return
        try:
            fi  = yf.Ticker(sym.replace(".", "-")).fast_info
            cap = getattr(fi, "market_cap", None)
            shares = getattr(fi, "shares", None)
            store.upsert_fundamental(sym, status="yfinance",
                                     market_cap=cap, shares_outstanding=shares)
        except Exception as e:
            store.upsert_fundamental(sym, status="failed", error=str(e))
            _fail(sym, e)
        _tick()
        time.sleep(YF_THROTTLE)

    # Phase 3 — earnings dates, focus list only (small, refreshed daily)
    need_earnings = store.stale_fundamental_symbols(focus, 1, column="earnings_date")
    _phase("earnings dates", need_earnings,
           f"earnings dates: {len(need_earnings)} focus symbols…")
    for sym in need_earnings:
        if _stop.is_set():
            return
        try:
            cal = yf.Ticker(sym.replace(".", "-")).calendar or {}
            dates = cal.get("Earnings Date") or []
            if not isinstance(dates, (list, tuple)):
                dates = [dates]
            future = sorted(str(d) for d in dates
                            if str(d) >= datetime.now().strftime("%Y-%m-%d"))
            store.upsert_fundamental(sym, status="yfinance",
                                     earnings_date=future[0] if future else None)
        except Exception as e:
            _fail(sym, e)
        _tick()
        time.sleep(YF_THROTTLE)

    # Phase 4 — sector classification long tail (only ever fetched once)
    with store.connect() as conn:
        have = {r[0] for r in conn.execute(
            "SELECT symbol FROM fundamentals WHERE sector IS NOT NULL "
            "OR status='failed'").fetchall()}
    no_sector = [s for s in symbols if s not in have]
    _phase("sectors", no_sector, f"sector lookup: {len(no_sector)} symbols…")
    from modules.schwab import SECTOR_ETF_MAP
    for sym in no_sector:
        if _stop.is_set():
            return
        try:
            sector = (yf.Ticker(sym.replace(".", "-")).info or {}).get("sector")
            store.upsert_fundamental(sym, status="yfinance", sector=sector,
                                     sector_etf=SECTOR_ETF_MAP.get(sector))
        except Exception as e:
            _fail(sym, e)
        _tick()
        time.sleep(YF_THROTTLE)

    _set(message="fundamentals refresh done")


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _run(kind):
    try:
        if kind in ("snapshot", "both"):
            _run_snapshot()
        if kind in ("fundamentals", "both") and not _stop.is_set():
            _run_fundamentals()
        if kind == "flagstats" and not _stop.is_set():
            _run_flagstats()
        if _stop.is_set():
            _set(state="done", message="stopped — re-run to resume",
                 finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        else:
            with _lock:
                failed = _state["failed"]
                msg    = _state["message"]
            if failed:
                msg += f" ({failed} symbols failed)"
            _set(state="done", message=msg,
                 finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        _set(state="error", message=str(e),
             finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
