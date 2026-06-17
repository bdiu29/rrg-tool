"""
SQLite store for the news / macro-events module.

Calendar + Fed events are small and slow-moving, so this store is mostly a
dedupe + accumulation layer: each refresh upserts fetched events keyed on
(source, kind, event_date, title) so re-fetching never duplicates, forward
calendar entries get their detail/date refreshed, and backward items (Fed
speeches/press releases) accumulate a history. Same WAL + per-call-connection
conventions as the breadth/screener stores.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH   = _DATA_DIR / "news.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,         -- fomc | fred | fed_rss | earnings | ...
    kind       TEXT NOT NULL,         -- fomc | econ | fed_news | fed_speech | earnings
    event_date TEXT NOT NULL,         -- YYYY-MM-DD
    event_time TEXT,                  -- HH:MM ET, when known (else NULL)
    title      TEXT NOT NULL,
    detail     TEXT,
    importance TEXT,                  -- high | med | low
    symbols    TEXT,                  -- comma-separated tickers, when relevant
    url        TEXT,
    extra      TEXT,                  -- JSON blob (earnings: when/eps_est/eps_actual/period)
    fetched_at TEXT,
    UNIQUE(source, kind, event_date, title)
);

CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);

CREATE TABLE IF NOT EXISTS settings (
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
        # ALTER-ADD migration for stores created before `extra` existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
        if "extra" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN extra TEXT")


def upsert_events(events):
    """Insert each event; on the (source, kind, event_date, title) key refresh the
    mutable fields (a release date detail/time may be revised between fetches).
    Returns the number of rows touched."""
    if not events:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [(
        e["source"], e["kind"], e["event_date"], e.get("event_time"),
        e["title"], e.get("detail"), e.get("importance", "low"),
        e.get("symbols"), e.get("url"), e.get("extra"), now,
    ) for e in events]
    with connect() as conn:
        conn.executemany(
            """INSERT INTO events
                   (source, kind, event_date, event_time, title, detail,
                    importance, symbols, url, extra, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, kind, event_date, title) DO UPDATE SET
                   event_time = excluded.event_time,
                   detail     = excluded.detail,
                   importance = excluded.importance,
                   symbols    = excluded.symbols,
                   url        = excluded.url,
                   extra      = excluded.extra,
                   fetched_at = excluded.fetched_at""",
            rows,
        )
    return len(rows)


def get_events(start_date, end_date, importance=None, kinds=None):
    """Events with start_date <= event_date <= end_date, oldest first.
    `importance` (list) and `kinds` (list) are optional filters."""
    sql    = "SELECT source, kind, event_date, event_time, title, detail, " \
             "importance, symbols, url, extra FROM events WHERE event_date BETWEEN ? AND ?"
    params = [start_date, end_date]
    if importance:
        sql += f" AND importance IN ({','.join('?' * len(importance))})"
        params += list(importance)
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    sql += " ORDER BY event_date ASC, importance DESC"
    with connect() as conn:
        cur  = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_setting(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
