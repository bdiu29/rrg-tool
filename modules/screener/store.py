"""
SQLite store for the screener module.

Owns watchlists, saved screens, alerts, the daily indicator snapshot, and the
fundamentals cache. Daily bars are NOT duplicated here — they are read from
the breadth module's store. WAL mode + per-call connections keep the snapshot
job, the intraday poller, and request threads from stepping on each other.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH   = _DATA_DIR / "screener.db"

_SCHEMA = """
-- One row per symbol, recomputed wholesale from breadth bars after each sync.
-- high/low levels are PRIOR-day rolling extremes (shift(1)) so a cross by
-- today's price is detectable against them.
CREATE TABLE IF NOT EXISTS snapshot (
    symbol  TEXT PRIMARY KEY,
    date    TEXT,                   -- bar date this row reflects
    close REAL, open REAL, volume REAL,
    chg_pct REAL, gap_pct REAL, vol_chg_pct REAL,
    avg_vol_10d REAL, rvol_10d REAL,
    sma20 REAL, sma50 REAL, sma150 REAL, sma200 REAL,
    rsi14 REAL, atr14 REAL, atr_pct REAL,
    rs_1m_pct REAL, rs_3m_pct REAL,
    high_20d REAL, low_20d REAL, high_252 REAL, low_252 REAL,
    pct_from_52w_high REAL, pct_from_52w_low REAL
);

-- Schwab instruments fills the numeric columns; yfinance fills the gaps plus
-- sector (full universe) and earnings_date (focus list only). status records
-- which source produced the row: schwab | yfinance | failed.
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol TEXT PRIMARY KEY,
    market_cap REAL, shares_outstanding REAL, avg_vol_10d_f REAL,
    pe_ratio REAL, div_yield REAL, beta REAL,
    sector TEXT, sector_etf TEXT,
    earnings_date TEXT,             -- YYYY-MM-DD
    updated_at TEXT, status TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS screens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    universe   TEXT DEFAULT 'all',  -- all | sp500 | nyse | nasdaq
    conditions TEXT NOT NULL,       -- JSON [{field, op, value}]
    armed      INTEGER DEFAULT 0,
    builtin    INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS watchlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    channels   TEXT DEFAULT '[]',   -- JSON subset of ["discord", "email"]
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS watchlist_symbols (
    watchlist_id INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    added_at     TEXT,
    PRIMARY KEY (watchlist_id, symbol)
);

-- UNIQUE(date, symbol, rule_key) + INSERT OR IGNORE is the dedupe: at most
-- one alert per symbol per rule per day, and external channels only ever see
-- newly inserted rows.
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL,
    ts           TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    rule_key     TEXT NOT NULL,
    kind         TEXT NOT NULL,     -- pump | dump | info
    message      TEXT,
    detail       TEXT,              -- JSON of trigger values
    delivered    TEXT DEFAULT '[]', -- JSON: channels actually sent
    acknowledged INTEGER DEFAULT 0,
    UNIQUE (date, symbol, rule_key)
);

-- Armed-screen memory: a screen alert fires only when a focus symbol is in
-- the match set now but wasn't on the previous evaluation.
CREATE TABLE IF NOT EXISTS screen_matches (
    screen_id     INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    first_matched TEXT,
    last_matched  TEXT,
    PRIMARY KEY (screen_id, symbol)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# The user's real TradingView presets, shipped as built-in screens.
BUILTIN_SCREENS = [
    {
        "name": "Breakout",
        "universe": "all",
        "conditions": [
            {"field": "market_cap",           "op": "between", "value": [1e7, 1e13]},
            {"field": "vol_chg_pct",          "op": ">",       "value": 80},
            {"field": "volume",               "op": ">",       "value": 100000},
            {"field": "price_vs_sma50_pct",   "op": ">=",      "value": 0},
            {"field": "rvol_10d",             "op": ">",       "value": 1.0},
        ],
    },
    {
        "name": "Continuation",
        "universe": "all",
        "conditions": [
            {"field": "price_vs_sma50_pct",   "op": ">",       "value": 0},
            {"field": "chg_pct",              "op": ">",       "value": 2},
            {"field": "market_cap",           "op": "between", "value": [1e8, 1e13]},
            {"field": "vol_chg_pct",          "op": ">",       "value": 80},
            {"field": "volume",               "op": ">",       "value": 100000},
            {"field": "price_vs_sma150_pct",  "op": ">",       "value": 0},
        ],
    },
]


def connect():
    _DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(_SCHEMA)
    prune_alerts()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

def get_meta(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def get_positions_channels():
    """Channels for alerts on position symbols not covered by a watchlist."""
    try:
        return set(json.loads(get_meta("positions_channels", "[]")))
    except (TypeError, json.JSONDecodeError):
        return set()


def set_positions_channels(channels):
    set_meta("positions_channels", json.dumps(sorted(set(channels))))


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

SNAPSHOT_COLS = [
    "symbol", "date", "close", "open", "volume",
    "chg_pct", "gap_pct", "vol_chg_pct",
    "avg_vol_10d", "rvol_10d",
    "sma20", "sma50", "sma150", "sma200",
    "rsi14", "atr14", "atr_pct",
    "rs_1m_pct", "rs_3m_pct",
    "high_20d", "low_20d", "high_252", "low_252",
    "pct_from_52w_high", "pct_from_52w_low",
]


def replace_snapshot(df):
    """df: one row per symbol with SNAPSHOT_COLS columns. Wholesale replace."""
    if df is None or df.empty:
        return 0
    df   = df.reindex(columns=SNAPSHOT_COLS)
    rows = list(df.itertuples(index=False))
    ph   = ",".join("?" * len(SNAPSHOT_COLS))
    with connect() as conn:
        conn.execute("DELETE FROM snapshot")
        conn.executemany(
            f"INSERT INTO snapshot ({','.join(SNAPSHOT_COLS)}) VALUES ({ph})",
            rows,
        )
    return len(rows)


def get_snapshot():
    with connect() as conn:
        df = pd.read_sql_query("SELECT * FROM snapshot", conn, index_col="symbol")
    return df


def snapshot_info():
    with connect() as conn:
        row = conn.execute("SELECT MAX(date), COUNT(*) FROM snapshot").fetchone()
    return {"date": row[0], "n_symbols": row[1] or 0}


# ---------------------------------------------------------------------------
# fundamentals
# ---------------------------------------------------------------------------

def get_fundamentals():
    with connect() as conn:
        df = pd.read_sql_query(
            "SELECT symbol, market_cap, pe_ratio, div_yield, beta, "
            "sector, sector_etf, earnings_date FROM fundamentals",
            conn, index_col="symbol",
        )
    return df


def upsert_fundamental(symbol, status, error=None, **fields):
    cols = ["symbol", "updated_at", "status", "error"] + list(fields)
    vals = [symbol, _now(), status, error] + list(fields.values())
    sets = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "symbol")
    with connect() as conn:
        conn.execute(
            f"INSERT INTO fundamentals ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(symbol) DO UPDATE SET {sets}",
            vals,
        )


def fundamentals_coverage():
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN market_cap IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN sector IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM fundamentals"
        ).fetchone()
    return {"rows": row[0] or 0, "market_cap": row[1] or 0, "sector": row[2] or 0}


def stale_fundamental_symbols(symbols, max_age_days=7, column=None):
    """Subset of `symbols` whose fundamentals row is missing, failed, stale,
    or (when `column` is given) has that column NULL."""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).strftime("%Y-%m-%d %H:%M:%S")
    fresh  = set()
    col_ok = f"AND {column} IS NOT NULL" if column else ""
    with connect() as conn:
        for i in range(0, len(symbols), 500):
            chunk = symbols[i:i + 500]
            ph    = ",".join("?" * len(chunk))
            rows  = conn.execute(
                f"SELECT symbol FROM fundamentals WHERE symbol IN ({ph}) "
                f"AND status != 'failed' AND updated_at >= ? {col_ok}",
                (*chunk, cutoff),
            ).fetchall()
            fresh.update(r[0] for r in rows)
    return [s for s in symbols if s not in fresh]


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------

def _screen_row(r):
    return {
        "id": r[0], "name": r[1], "universe": r[2],
        "conditions": json.loads(r[3]), "armed": bool(r[4]), "builtin": bool(r[5]),
    }


def list_screens(armed_only=False):
    q = "SELECT id, name, universe, conditions, armed, builtin FROM screens"
    if armed_only:
        q += " WHERE armed=1"
    with connect() as conn:
        rows = conn.execute(q + " ORDER BY builtin DESC, name").fetchall()
    return [_screen_row(r) for r in rows]


def get_screen(screen_id):
    with connect() as conn:
        r = conn.execute(
            "SELECT id, name, universe, conditions, armed, builtin "
            "FROM screens WHERE id=?", (screen_id,),
        ).fetchone()
    return _screen_row(r) if r else None


def save_screen(name, universe, conditions, screen_id=None, armed=None):
    """Create (screen_id=None) or update. Returns the screen id."""
    cond_json = json.dumps(conditions)
    with connect() as conn:
        if screen_id is None:
            cur = conn.execute(
                "INSERT INTO screens (name, universe, conditions, armed, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, universe, cond_json, int(bool(armed)), _now(), _now()),
            )
            return cur.lastrowid
        conn.execute(
            "UPDATE screens SET name=?, universe=?, conditions=?, updated_at=? "
            + (", armed=?" if armed is not None else "")
            + " WHERE id=?",
            (name, universe, cond_json, _now())
            + ((int(bool(armed)),) if armed is not None else ())
            + (screen_id,),
        )
        return screen_id


def set_screen_armed(screen_id, armed):
    with connect() as conn:
        conn.execute("UPDATE screens SET armed=?, updated_at=? WHERE id=?",
                     (int(bool(armed)), _now(), screen_id))


def delete_screen(screen_id):
    with connect() as conn:
        conn.execute("DELETE FROM screens WHERE id=?", (screen_id,))
        conn.execute("DELETE FROM screen_matches WHERE screen_id=?", (screen_id,))


def seed_builtin_screens():
    with connect() as conn:
        for spec in BUILTIN_SCREENS:
            exists = conn.execute(
                "SELECT 1 FROM screens WHERE name=?", (spec["name"],)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO screens (name, universe, conditions, builtin, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, ?)",
                    (spec["name"], spec["universe"], json.dumps(spec["conditions"]),
                     _now(), _now()),
                )


# ---------------------------------------------------------------------------
# watchlists
# ---------------------------------------------------------------------------

def list_watchlists():
    with connect() as conn:
        lists = conn.execute(
            "SELECT id, name, channels FROM watchlists ORDER BY name"
        ).fetchall()
        syms = conn.execute(
            "SELECT watchlist_id, symbol FROM watchlist_symbols ORDER BY symbol"
        ).fetchall()
    by_list = {}
    for wid, sym in syms:
        by_list.setdefault(wid, []).append(sym)
    return [
        {"id": wid, "name": name, "channels": json.loads(channels or "[]"),
         "symbols": by_list.get(wid, [])}
        for wid, name, channels in lists
    ]


def save_watchlist(name, symbols, channels=None, watchlist_id=None):
    """Create (watchlist_id=None) or update; symbols replace wholesale.
    Returns the watchlist id."""
    symbols  = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    channels = json.dumps(sorted(set(channels or [])))
    with connect() as conn:
        if watchlist_id is None:
            cur = conn.execute(
                "INSERT INTO watchlists (name, channels, created_at) VALUES (?, ?, ?)",
                (name, channels, _now()),
            )
            watchlist_id = cur.lastrowid
        else:
            conn.execute("UPDATE watchlists SET name=?, channels=? WHERE id=?",
                         (name, channels, watchlist_id))
            conn.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=?",
                         (watchlist_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist_symbols (watchlist_id, symbol, added_at) "
            "VALUES (?, ?, ?)",
            [(watchlist_id, s, _now()) for s in symbols],
        )
    return watchlist_id


def delete_watchlist(watchlist_id):
    with connect() as conn:
        conn.execute("DELETE FROM watchlists WHERE id=?", (watchlist_id,))
        conn.execute("DELETE FROM watchlist_symbols WHERE watchlist_id=?",
                     (watchlist_id,))


def all_watchlist_symbols():
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT symbol FROM watchlist_symbols").fetchall()
    return sorted(r[0] for r in rows)


def channels_for_symbols(symbols):
    """{symbol: set(channels)} — union across every watchlist holding it."""
    out = {s: set() for s in symbols}
    if not symbols:
        return out
    rows = []
    with connect() as conn:
        for i in range(0, len(symbols), 500):
            chunk = symbols[i:i + 500]
            ph    = ",".join("?" * len(chunk))
            rows.extend(conn.execute(
                f"SELECT ws.symbol, w.channels FROM watchlist_symbols ws "
                f"JOIN watchlists w ON w.id = ws.watchlist_id "
                f"WHERE ws.symbol IN ({ph})",
                chunk,
            ).fetchall())
    for sym, channels in rows:
        out[sym].update(json.loads(channels or "[]"))
    return out


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------

def insert_alert(date, symbol, rule_key, kind, message, detail=None):
    """Alert id if newly inserted (first firing of this rule today), else None."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO alerts (date, ts, symbol, rule_key, kind, message, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, _now(), symbol, rule_key, kind, message,
             json.dumps(detail or {})),
        )
        return cur.lastrowid if cur.rowcount > 0 else None


def mark_delivered(alert_ids, channels):
    if not alert_ids or not channels:
        return
    payload = json.dumps(sorted(set(channels)))
    with connect() as conn:
        conn.executemany(
            "UPDATE alerts SET delivered=? WHERE id=?",
            [(payload, aid) for aid in alert_ids],
        )


def list_alerts(days=5, unacked_only=False):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = ("SELECT id, date, ts, symbol, rule_key, kind, message, detail, "
         "delivered, acknowledged FROM alerts WHERE date >= ?")
    if unacked_only:
        q += " AND acknowledged=0"
    with connect() as conn:
        rows = conn.execute(q + " ORDER BY ts DESC", (cutoff,)).fetchall()
    return [
        {"id": r[0], "date": r[1], "ts": r[2], "symbol": r[3], "rule_key": r[4],
         "kind": r[5], "message": r[6], "detail": json.loads(r[7] or "{}"),
         "delivered": json.loads(r[8] or "[]"), "acknowledged": bool(r[9])}
        for r in rows
    ]


def get_alerts_by_ids(alert_ids):
    if not alert_ids:
        return []
    ph = ",".join("?" * len(alert_ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, date, symbol, rule_key, kind, message, detail "
            f"FROM alerts WHERE id IN ({ph})", alert_ids,
        ).fetchall()
    return [
        {"id": r[0], "date": r[1], "symbol": r[2], "rule_key": r[3],
         "kind": r[4], "message": r[5], "detail": json.loads(r[6] or "{}")}
        for r in rows
    ]


def ack_alerts(ids=None, all_alerts=False):
    with connect() as conn:
        if all_alerts:
            conn.execute("UPDATE alerts SET acknowledged=1 WHERE acknowledged=0")
        elif ids:
            ph = ",".join("?" * len(ids))
            conn.execute(f"UPDATE alerts SET acknowledged=1 WHERE id IN ({ph})", ids)


def alerts_summary():
    today = datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        n_today  = conn.execute("SELECT COUNT(*) FROM alerts WHERE date=?", (today,)).fetchone()[0]
        n_unack  = conn.execute("SELECT COUNT(*) FROM alerts WHERE acknowledged=0").fetchone()[0]
        rows = conn.execute(
            "SELECT symbol, kind, rule_key FROM alerts WHERE acknowledged=0 ORDER BY ts DESC"
        ).fetchall()
    by_symbol = {}
    for sym, kind, rule in rows:
        by_symbol.setdefault(sym, []).append({"kind": kind, "rule_key": rule})
    return {"today": n_today, "unacked": n_unack, "by_symbol": by_symbol}


def prune_alerts(days=60):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with connect() as conn:
        conn.execute("DELETE FROM alerts WHERE date < ?", (cutoff,))


# ---------------------------------------------------------------------------
# armed-screen match memory
# ---------------------------------------------------------------------------

def get_screen_matches(screen_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol FROM screen_matches WHERE screen_id=?", (screen_id,)
        ).fetchall()
    return {r[0] for r in rows}


def update_screen_matches(screen_id, symbols, date):
    """Replace the match set; keeps first_matched for symbols still matching."""
    symbols = set(symbols)
    with connect() as conn:
        existing = {
            r[0] for r in conn.execute(
                "SELECT symbol FROM screen_matches WHERE screen_id=?", (screen_id,)
            ).fetchall()
        }
        gone = existing - symbols
        if gone:
            ph = ",".join("?" * len(gone))
            conn.execute(
                f"DELETE FROM screen_matches WHERE screen_id=? AND symbol IN ({ph})",
                (screen_id, *gone),
            )
        conn.executemany(
            "INSERT INTO screen_matches (screen_id, symbol, first_matched, last_matched) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(screen_id, symbol) DO UPDATE SET last_matched=excluded.last_matched",
            [(screen_id, s, date, date) for s in symbols],
        )
