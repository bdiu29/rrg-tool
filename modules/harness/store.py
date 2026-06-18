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

CREATE TABLE IF NOT EXISTS paper_account (
    book           TEXT PRIMARY KEY,   -- 'long_only' | 'hedged'
    cash           REAL,
    start_equity   REAL,
    inception_date TEXT
);

CREATE TABLE IF NOT EXISTS paper_position (
    book     TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    shares   REAL,
    avg_cost REAL,
    stop     REAL,
    PRIMARY KEY (book, symbol)
);

CREATE TABLE IF NOT EXISTS paper_step (
    date      TEXT NOT NULL,
    book      TEXT NOT NULL,
    equity    REAL, gross REAL, net REAL, cash REAL,
    turnover  REAL, cost_paid REAL, n_long INTEGER,
    score     REAL, stance TEXT, spy REAL, rsp REAL,
    PRIMARY KEY (date, book)             -- idempotency: one rebalance per book per day
);

CREATE TABLE IF NOT EXISTS paper_fill (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT, book TEXT, symbol TEXT, side TEXT,
    shares   REAL, price REAL, notional REAL, cost REAL, reason TEXT
);

CREATE TABLE IF NOT EXISTS paper_decision (
    date      TEXT PRIMARY KEY,
    stance    TEXT, score REAL, posture TEXT, picks_json TEXT
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


# ---------------------------------------------------------------------------
# Paper books (Layer B)
# ---------------------------------------------------------------------------

def _rows(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_account(book):
    with connect() as conn:
        rows = _rows(conn, "SELECT * FROM paper_account WHERE book=?", (book,))
    return rows[0] if rows else None


def init_account(book, start_equity, inception):
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO paper_account (book, cash, start_equity, inception_date) "
            "VALUES (?, ?, ?, ?)", (book, start_equity, start_equity, inception))
    return get_account(book)


def update_cash(book, cash):
    with connect() as conn:
        conn.execute("UPDATE paper_account SET cash=? WHERE book=?", (cash, book))


def get_positions(book):
    """{symbol: {shares, avg_cost, stop}}."""
    with connect() as conn:
        rows = _rows(conn, "SELECT symbol, shares, avg_cost, stop FROM paper_position "
                           "WHERE book=?", (book,))
    return {r["symbol"]: {"shares": r["shares"], "avg_cost": r["avg_cost"],
                          "stop": r["stop"]} for r in rows}


def replace_positions(book, positions):
    with connect() as conn:
        conn.execute("DELETE FROM paper_position WHERE book=?", (book,))
        conn.executemany(
            "INSERT INTO paper_position (book, symbol, shares, avg_cost, stop) "
            "VALUES (?, ?, ?, ?, ?)",
            [(book, s, p["shares"], p.get("avg_cost"), p.get("stop"))
             for s, p in positions.items()])


def add_fills(date, book, fills):
    """fills: [(symbol, side, shares, price, notional, cost, reason)]."""
    if not fills:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT INTO paper_fill (date, book, symbol, side, shares, price, notional, "
            "cost, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(date, book) + tuple(f) for f in fills])


def step_exists(date, book):
    with connect() as conn:
        return conn.execute("SELECT 1 FROM paper_step WHERE date=? AND book=?",
                            (date, book)).fetchone() is not None


def record_step(row):
    """row: dict with date/book/equity/gross/net/cash/turnover/cost_paid/n_long/
    score/stance/spy/rsp. INSERT OR REPLACE (idempotent re-step overwrites)."""
    cols = ("date", "book", "equity", "gross", "net", "cash", "turnover",
            "cost_paid", "n_long", "score", "stance", "spy", "rsp")
    with connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO paper_step ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", tuple(row.get(c) for c in cols))


def record_decision(date, stance, score, posture, picks_json):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO paper_decision (date, stance, score, posture, picks_json) "
            "VALUES (?, ?, ?, ?, ?)", (date, stance, score, posture, picks_json))


def get_steps(book):
    """All step rows for a book, ascending by date."""
    with connect() as conn:
        return _rows(conn, "SELECT * FROM paper_step WHERE book=? ORDER BY date", (book,))


def get_fills(book, limit=50):
    with connect() as conn:
        return _rows(conn, "SELECT * FROM paper_fill WHERE book=? ORDER BY id DESC LIMIT ?",
                     (book, limit))


def reset_paper():
    """Wipe all paper books (caller guards this)."""
    with connect() as conn:
        for t in ("paper_account", "paper_position", "paper_step", "paper_fill",
                  "paper_decision"):
            conn.execute(f"DELETE FROM {t}")
