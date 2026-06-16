"""
SQLite store for the options-flow module (WAL, per-call connections — the same
conventions as breadth/screener). Holds:

  * contract_state — per-contract running state within a day (last seen volume +
    cluster_count) so the poller can diff snapshots into volume deltas / bursts.
  * flow_signal    — one upserted row per flagged contract per day (the feed).
  * oi_history     — daily open interest, for the next-morning entered/exited read.
  * ticker_baseline— daily total options notional per underlying → a trailing avg
    (the "typical daily options flow" Rule 2 compares against).
  * flow_alert     — dispatched alerts, deduped UNIQUE(date, option_symbol, rule_key).
  * settings       — universe / thresholds / channels / source / interval.

Bars are never stored here; this module only persists options-derived state.
"""

import json
import sqlite3
import threading
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent / "data" / "flow.db"
_INIT_LOCK = threading.Lock()
_INITED = False


def _conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db():
    global _INITED
    with _INIT_LOCK:
        if _INITED:
            return
        with _conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS contract_state (
                option_symbol TEXT PRIMARY KEY,
                underlying    TEXT, put_call TEXT, strike REAL, expiry TEXT,
                last_volume   INTEGER DEFAULT 0,
                cluster_count INTEGER DEFAULT 0,
                day           TEXT
            );
            CREATE TABLE IF NOT EXISTS flow_signal (
                date TEXT, option_symbol TEXT,
                underlying TEXT, put_call TEXT, strike REAL, expiry TEXT, dte INTEGER,
                expiry_bucket TEXT, ts TEXT, first_ts TEXT,
                session_volume INTEGER, open_interest INTEGER, vol_oi_ratio REAL,
                notional REAL, notional_vs_baseline REAL, moneyness REAL,
                direction TEXT, aggressor TEXT, aggressor_method TEXT, cluster_count INTEGER,
                conviction REAL, classification TEXT,
                factors_json TEXT, confluence_json TEXT,
                entry_exit TEXT,
                PRIMARY KEY (date, option_symbol)
            );
            CREATE INDEX IF NOT EXISTS ix_signal_date ON flow_signal(date, conviction DESC);
            CREATE TABLE IF NOT EXISTS oi_history (
                option_symbol TEXT, date TEXT, open_interest INTEGER,
                PRIMARY KEY (option_symbol, date)
            );
            CREATE TABLE IF NOT EXISTS ticker_baseline (
                underlying TEXT, date TEXT, opt_notional REAL,
                PRIMARY KEY (underlying, date)
            );
            CREATE TABLE IF NOT EXISTS flow_alert (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, option_symbol TEXT, rule_key TEXT,
                kind TEXT, message TEXT, detail TEXT,
                created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
                delivered TEXT,
                UNIQUE(date, option_symbol, rule_key)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            """)
        _INITED = True


# --- diff / cluster state ---------------------------------------------------

def record_poll(c, day, burst_notional):
    """Update per-contract running state with this poll's snapshot and return
    (volume_delta, cluster_count). A "burst" = a poll whose new volume carries
    ≥ `burst_notional` of premium; cluster_count tallies bursts across the day
    (the trader's repeated-prints/accumulation tell). State resets each new day."""
    init_db()
    osym = c["option_symbol"]
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_volume, cluster_count, day FROM contract_state WHERE option_symbol=?",
            (osym,)).fetchone()
        if row is None or row["day"] != day:
            prev_vol, cluster = 0, 0
        else:
            prev_vol, cluster = row["last_volume"], row["cluster_count"]
        svol = int(c.get("session_volume") or 0)
        vol_delta = max(0, svol - prev_vol)
        mark = c.get("mark") or c.get("last") or 0
        if vol_delta * mark * 100 >= burst_notional:
            cluster += 1
        conn.execute("""
            INSERT INTO contract_state
                (option_symbol, underlying, put_call, strike, expiry, last_volume, cluster_count, day)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(option_symbol) DO UPDATE SET
                last_volume=excluded.last_volume, cluster_count=excluded.cluster_count,
                day=excluded.day, underlying=excluded.underlying, put_call=excluded.put_call,
                strike=excluded.strike, expiry=excluded.expiry
        """, (osym, c.get("underlying"), c.get("put_call"), c.get("strike"),
              c.get("expiry"), svol, cluster, day))
        return vol_delta, cluster


# --- flow signals -----------------------------------------------------------

def upsert_flow_signal(date, c, r, confluence):
    """Insert/refresh the flagged-contract row for the day. Preserves first_ts and
    a non-null entry_exit set later by the OI-confirmation pass."""
    init_db()
    with _conn() as conn:
        prev = conn.execute(
            "SELECT first_ts, entry_exit FROM flow_signal WHERE date=? AND option_symbol=?",
            (date, c["option_symbol"])).fetchone()
        first_ts = (prev["first_ts"] if prev else None) or c.get("ts")
        entry_exit = prev["entry_exit"] if prev else None
        conn.execute("""
            INSERT INTO flow_signal
              (date, option_symbol, underlying, put_call, strike, expiry, dte, expiry_bucket,
               ts, first_ts, session_volume, open_interest, vol_oi_ratio, notional,
               notional_vs_baseline, moneyness, direction, aggressor, aggressor_method,
               cluster_count, conviction, classification, factors_json, confluence_json, entry_exit)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(date, option_symbol) DO UPDATE SET
              ts=excluded.ts, session_volume=excluded.session_volume,
              open_interest=excluded.open_interest, vol_oi_ratio=excluded.vol_oi_ratio,
              notional=excluded.notional, notional_vs_baseline=excluded.notional_vs_baseline,
              direction=excluded.direction, aggressor=excluded.aggressor,
              aggressor_method=excluded.aggressor_method, cluster_count=excluded.cluster_count,
              conviction=excluded.conviction, classification=excluded.classification,
              factors_json=excluded.factors_json, confluence_json=excluded.confluence_json
        """, (date, c["option_symbol"], c.get("underlying"), c.get("put_call"), c.get("strike"),
              c.get("expiry"), c.get("dte"), r.get("expiry_bucket"), c.get("ts"), first_ts,
              c.get("session_volume"), c.get("open_interest"), r.get("vol_oi_ratio"),
              r.get("notional"), r.get("notional_vs_baseline"), r.get("moneyness"),
              r.get("direction"), r.get("aggressor"), r.get("aggressor_method"),
              c.get("cluster_count"), r.get("conviction"), r.get("classification"),
              json.dumps(r.get("factors")), json.dumps(confluence or {}), entry_exit))


def list_flow_signals(date, min_conviction=0, classification=None, side=None,
                      bucket=None, underlying=None, limit=300):
    init_db()
    q = "SELECT * FROM flow_signal WHERE date=? AND conviction>=?"
    args = [date, min_conviction]
    if classification:
        q += " AND classification=?"; args.append(classification)
    if side in ("bullish", "bearish"):
        q += " AND direction=?"; args.append(side)
    if bucket:
        q += " AND expiry_bucket=?"; args.append(bucket)
    if underlying:
        q += " AND underlying=?"; args.append(underlying.upper())
    q += " ORDER BY conviction DESC LIMIT ?"; args.append(limit)
    with _conn() as conn:
        return [_signal_row(r) for r in conn.execute(q, args).fetchall()]


def latest_signal_date(default=None):
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT MAX(date) AS d FROM flow_signal").fetchone()
    return (row["d"] if row and row["d"] else default)


def get_flow_signal(date, option_symbol):
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM flow_signal WHERE date=? AND option_symbol=?",
                           (date, option_symbol)).fetchone()
    return _signal_row(row) if row else None


def unconfirmed_before(before_date):
    """Signals from PRIOR days still lacking an entry/exit read — input to the
    next-morning OI-confirmation pass (OI for a session prints the next morning)."""
    init_db()
    with _conn() as conn:
        return [_signal_row(r) for r in conn.execute(
            "SELECT * FROM flow_signal WHERE date<? AND entry_exit IS NULL", (before_date,)).fetchall()]


def oi_on(option_symbol, date):
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT open_interest FROM oi_history WHERE option_symbol=? AND date=?",
                           (option_symbol, date)).fetchone()
    return row["open_interest"] if row else None


def set_entry_exit(date, option_symbol, entry_exit):
    init_db()
    with _conn() as conn:
        conn.execute("UPDATE flow_signal SET entry_exit=? WHERE date=? AND option_symbol=?",
                     (entry_exit, date, option_symbol))


def _signal_row(r):
    d = dict(r)
    d["factors"] = json.loads(d.pop("factors_json") or "[]")
    d["confluence"] = json.loads(d.pop("confluence_json") or "{}")
    return d


# --- open interest + ticker baseline ---------------------------------------

def record_oi(option_symbol, date, oi):
    init_db()
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO oi_history(option_symbol, date, open_interest) VALUES (?,?,?)",
                     (option_symbol, date, oi))


def prior_oi(option_symbol, before_date):
    init_db()
    with _conn() as conn:
        row = conn.execute(
            "SELECT open_interest FROM oi_history WHERE option_symbol=? AND date<? ORDER BY date DESC LIMIT 1",
            (option_symbol, before_date)).fetchone()
    return row["open_interest"] if row else None


def record_ticker_notional(underlying, date, notional):
    init_db()
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO ticker_baseline(underlying, date, opt_notional) VALUES (?,?,?)",
                     (underlying.upper(), date, notional))


def ticker_baseline(underlying, before_date, lookback=20):
    """Trailing average daily options notional for an underlying, EXCLUDING today —
    the "typical daily options flow" Rule 2 compares against. None until history exists."""
    init_db()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT opt_notional FROM ticker_baseline WHERE underlying=? AND date<? ORDER BY date DESC LIMIT ?",
            (underlying.upper(), before_date, lookback)).fetchall()
    vals = [r["opt_notional"] for r in rows if r["opt_notional"]]
    return (sum(vals) / len(vals)) if vals else None


# --- alerts -----------------------------------------------------------------

def insert_alert(date, option_symbol, rule_key, kind, message, detail):
    """Dedupe is structural: UNIQUE(date, option_symbol, rule_key) + INSERT OR IGNORE,
    so external channels only ever see a brand-new row. Returns the id or None."""
    init_db()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO flow_alert(date, option_symbol, rule_key, kind, message, detail) "
            "VALUES (?,?,?,?,?,?)",
            (date, option_symbol, rule_key, kind, message, json.dumps(detail or {})))
        return cur.lastrowid if cur.rowcount else None


def list_alerts(limit=100):
    init_db()
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM flow_alert ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r); d["detail"] = json.loads(d["detail"] or "{}")
        out.append(d)
    return out


def mark_delivered(ids, channels):
    init_db()
    with _conn() as conn:
        conn.executemany("UPDATE flow_alert SET delivered=? WHERE id=?",
                         [(",".join(channels), i) for i in ids])


# --- settings ---------------------------------------------------------------

def get_setting(key, default=None):
    init_db()
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def set_setting(key, value):
    init_db()
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
                     (key, json.dumps(value)))


def all_settings():
    init_db()
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (TypeError, ValueError):
            out[r["key"]] = r["value"]
    return out
