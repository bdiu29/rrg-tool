"""
SQLite store for the breadth module.

All price history and computed breadth series live here so indicators never
re-pull full history from the network (forced by Schwab's 120/min rate limit
and per-symbol history calls). WAL mode + per-call connections keep the
background backfill thread and request threads from stepping on each other.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

_DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH   = _DATA_DIR / "breadth.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,          -- YYYY-MM-DD
    open    REAL, high REAL, low REAL, close REAL,
    volume  REAL,
    PRIMARY KEY (symbol, date)
);

-- Dated membership: constituents are a point-in-time input. Today's lists are
-- survivorship-biased for deep history; point-in-time lists can be imported
-- later by writing first_seen/last_seen explicitly.
CREATE TABLE IF NOT EXISTS members (
    universe   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    first_seen TEXT,
    last_seen  TEXT,
    active     INTEGER DEFAULT 1,
    PRIMARY KEY (universe, symbol)
);

CREATE TABLE IF NOT EXISTS sync_state (
    symbol     TEXT PRIMARY KEY,
    last_date  TEXT,                -- newest bar date stored
    status     TEXT,                -- ok | failed
    error      TEXT,
    updated_at TEXT
);

-- Per-day cross-sectional aggregates (the expensive part of every indicator).
CREATE TABLE IF NOT EXISTS breadth_daily (
    universe      TEXT NOT NULL,
    date          TEXT NOT NULL,
    advances      INTEGER, declines INTEGER, unchanged INTEGER,
    up_vol        REAL, down_vol REAL,
    new_highs     INTEGER, new_lows INTEGER,
    pct_above_20  REAL, pct_above_50 REAL, pct_above_200 REAL,
    n_symbols     INTEGER,
    PRIMARY KEY (universe, date)
);

-- Derived indicator chains, recomputed wholesale from breadth_daily after
-- each sync (cheap — a few thousand rows) and persisted to stay queryable.
CREATE TABLE IF NOT EXISTS indicator_values (
    universe     TEXT NOT NULL,
    date         TEXT NOT NULL,
    mcclellan    REAL, summation REAL, trin REAL,
    ad_line      REAL, ad_vol_line REAL,
    net_up_vol   REAL, ud_vol_ratio REAL,
    nh_nl        REAL, hl_index REAL, zbt_ema REAL,
    PRIMARY KEY (universe, date)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect():
    _DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(_SCHEMA)


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


# ---------------------------------------------------------------------------
# bars
# ---------------------------------------------------------------------------

def upsert_bars(symbol, df):
    """df: columns date, open, high, low, close, volume (date = YYYY-MM-DD)."""
    if df is None or df.empty:
        return 0
    rows = [
        (symbol, r.date, r.open, r.high, r.low, r.close, r.volume)
        for r in df.itertuples(index=False)
    ]
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bars (symbol, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def last_bar_date(symbol):
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(date) FROM bars WHERE symbol=?", (symbol,)
        ).fetchone()
    return row[0] if row and row[0] else None


def get_series(symbol, fields=("close",), start=None):
    """Single-symbol series as a date-indexed DataFrame."""
    cols = ", ".join(fields)
    q    = f"SELECT date, {cols} FROM bars WHERE symbol=?"
    args = [symbol]
    if start:
        q    += " AND date >= ?"
        args.append(start)
    q += " ORDER BY date"
    with connect() as conn:
        df = pd.read_sql_query(q, conn, params=args, index_col="date")
    return df


def get_panels(symbols, start=None, fields=("close", "volume")):
    """Field panels (date × symbol) for a symbol list, returned as a tuple in
    `fields` order, chunked to stay under SQLite's bound-variable limit."""
    cols   = ", ".join(fields)
    frames = []
    for i in range(0, len(symbols), 500):
        chunk = symbols[i:i + 500]
        ph    = ",".join("?" * len(chunk))
        q     = f"SELECT symbol, date, {cols} FROM bars WHERE symbol IN ({ph})"
        args  = list(chunk)
        if start:
            q    += " AND date >= ?"
            args.append(start)
        with connect() as conn:
            frames.append(pd.read_sql_query(q, conn, params=args))
    if not frames:
        return tuple(pd.DataFrame() for _ in fields)
    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        return tuple(pd.DataFrame() for _ in fields)
    return tuple(
        df.pivot(index="date", columns="symbol", values=f).sort_index()
        for f in fields
    )


def bar_counts(symbols):
    """{symbol: n_bars} for a symbol list."""
    out = {}
    for i in range(0, len(symbols), 500):
        chunk = symbols[i:i + 500]
        ph    = ",".join("?" * len(chunk))
        with connect() as conn:
            for sym, n in conn.execute(
                f"SELECT symbol, COUNT(*) FROM bars WHERE symbol IN ({ph}) GROUP BY symbol",
                chunk,
            ):
                out[sym] = n
    return out


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------

def upsert_members(universe, symbols):
    """Refresh membership: new symbols get first_seen=today, present symbols
    bump last_seen, absent symbols flip to active=0 (kept, dated)."""
    today = datetime.now().strftime("%Y-%m-%d")
    syms  = set(symbols)
    with connect() as conn:
        existing = {
            r[0] for r in conn.execute(
                "SELECT symbol FROM members WHERE universe=?", (universe,)
            )
        }
        conn.executemany(
            "INSERT INTO members (universe, symbol, first_seen, last_seen, active) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(universe, symbol) DO UPDATE SET last_seen=excluded.last_seen, active=1",
            [(universe, s, today, today) for s in syms],
        )
        gone = existing - syms
        if gone:
            conn.executemany(
                "UPDATE members SET active=0 WHERE universe=? AND symbol=?",
                [(universe, s) for s in gone],
            )
    set_meta(f"members_refreshed:{universe}", today)


def get_members(universe, active_only=True):
    q = "SELECT symbol FROM members WHERE universe=?"
    if active_only:
        q += " AND active=1"
    with connect() as conn:
        return [r[0] for r in conn.execute(q, (universe,))]


# ---------------------------------------------------------------------------
# sync state
# ---------------------------------------------------------------------------

def get_sync_states(symbols):
    out = {}
    for i in range(0, len(symbols), 500):
        chunk = symbols[i:i + 500]
        ph    = ",".join("?" * len(chunk))
        with connect() as conn:
            for sym, last, status in conn.execute(
                f"SELECT symbol, last_date, status FROM sync_state WHERE symbol IN ({ph})",
                chunk,
            ):
                out[sym] = {"last_date": last, "status": status}
    return out


def set_sync_state(symbol, last_date, status="ok", error=None):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (symbol, last_date, status, error, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol, last_date, status, error,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


# ---------------------------------------------------------------------------
# computed series
# ---------------------------------------------------------------------------

def replace_breadth_daily(universe, df):
    """df: date-indexed with breadth_daily's value columns."""
    cols = ["advances", "declines", "unchanged", "up_vol", "down_vol",
            "new_highs", "new_lows", "pct_above_20", "pct_above_50",
            "pct_above_200", "n_symbols"]
    rows = [
        tuple([universe, d] + [None if pd.isna(v) else float(v) for v in r])
        for d, r in zip(df.index, df[cols].to_numpy())
    ]
    with connect() as conn:
        conn.execute("DELETE FROM breadth_daily WHERE universe=?", (universe,))
        conn.executemany(
            f"INSERT INTO breadth_daily (universe, date, {', '.join(cols)}) "
            f"VALUES (?, ?, {', '.join('?' * len(cols))})",
            rows,
        )


def replace_indicator_values(universe, df):
    cols = ["mcclellan", "summation", "trin", "ad_line", "ad_vol_line",
            "net_up_vol", "ud_vol_ratio", "nh_nl", "hl_index", "zbt_ema"]
    rows = [
        tuple([universe, d] + [None if pd.isna(v) else float(v) for v in r])
        for d, r in zip(df.index, df[cols].to_numpy())
    ]
    with connect() as conn:
        conn.execute("DELETE FROM indicator_values WHERE universe=?", (universe,))
        conn.executemany(
            f"INSERT INTO indicator_values (universe, date, {', '.join(cols)}) "
            f"VALUES (?, ?, {', '.join('?' * len(cols))})",
            rows,
        )


def get_breadth_daily(universe, start=None):
    q, args = "SELECT * FROM breadth_daily WHERE universe=?", [universe]
    if start:
        q    += " AND date >= ?"
        args.append(start)
    q += " ORDER BY date"
    with connect() as conn:
        df = pd.read_sql_query(q, conn, params=args, index_col="date")
    return df.drop(columns=["universe"], errors="ignore")


def get_indicator_values(universe, start=None):
    q, args = "SELECT * FROM indicator_values WHERE universe=?", [universe]
    if start:
        q    += " AND date >= ?"
        args.append(start)
    q += " ORDER BY date"
    with connect() as conn:
        df = pd.read_sql_query(q, conn, params=args, index_col="date")
    return df.drop(columns=["universe"], errors="ignore")


def universe_status(universe):
    """Cache coverage summary for the dashboard's universe buttons."""
    with connect() as conn:
        n_members = conn.execute(
            "SELECT COUNT(*) FROM members WHERE universe=? AND active=1", (universe,)
        ).fetchone()[0]
        row = conn.execute(
            "SELECT COUNT(*), MAX(date) FROM breadth_daily WHERE universe=?", (universe,)
        ).fetchone()
        n_days, last_date = row[0], row[1]
        synced = 0
        if n_members:
            synced = conn.execute(
                "SELECT COUNT(*) FROM sync_state s JOIN members m ON s.symbol=m.symbol "
                "WHERE m.universe=? AND m.active=1 AND s.status='ok'", (universe,)
            ).fetchone()[0]
    return {
        "members":      n_members,
        "synced":       synced,
        "breadth_days": n_days,
        "last_date":    last_date,
        "refreshed":    get_meta(f"members_refreshed:{universe}"),
    }
