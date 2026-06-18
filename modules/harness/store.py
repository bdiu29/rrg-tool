"""
SQLite store for the harness trading layer (Phase 3).

Holds the user's uploaded **watchlist** (the paper/live trading focus) and — added
in Layer B — the paper-trading books. WAL + per-call connections, the same
conventions as the screener / themes / flow stores. Self-contained: the watchlist
here is decoupled from the screener's watchlists (which drive alert routing).
"""

import sqlite3
from datetime import datetime
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH   = _DATA_DIR / "trading.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT PRIMARY KEY,
    added_at TEXT,
    source   TEXT
);
"""


def connect():
    _DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with connect() as conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Watchlist (the trading focus)
# ---------------------------------------------------------------------------

def set_watchlist(symbols, source="upload", replace=True):
    """Store the focus watchlist. `replace=True` wipes the prior list first (a fresh
    upload); `replace=False` merges (add to the existing focus). Returns the count."""
    syms = []
    seen = set()
    for s in symbols:
        s = (s or "").strip().upper()
        if s and s not in seen:
            seen.add(s)
            syms.append(s)
    with connect() as conn:
        if replace:
            conn.execute("DELETE FROM watchlist")
        conn.executemany(
            "INSERT OR IGNORE INTO watchlist (symbol, added_at, source) VALUES (?, ?, ?)",
            [(s, _now(), source) for s in syms],
        )
        return conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]


def get_watchlist():
    """[symbol, …] ordered alphabetically."""
    with connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT symbol FROM watchlist ORDER BY symbol").fetchall()]


def remove_symbol(symbol):
    with connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE symbol=?", ((symbol or "").upper(),))


def clear_watchlist():
    with connect() as conn:
        conn.execute("DELETE FROM watchlist")
