"""
SQLite store for the themes module.

A theme is a named, user-editable basket of stock tickers. Storage mirrors the
screener's watchlist pattern (named list + symbols child table, CRUD that
replaces symbols wholesale). WAL mode + per-call connections, same conventions
as the other modules. Ten built-in themes are seeded on first init; the user
refines membership in the UI.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH   = _DATA_DIR / "themes.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS themes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    builtin     INTEGER DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS theme_symbols (
    theme_id INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    added_at TEXT,
    PRIMARY KEY (theme_id, symbol)
);
"""

# Curated starter baskets — representative, not exhaustive. The user refines
# these in the editor; seeding only happens when a theme name doesn't yet exist.
BUILTIN_THEMES = [
    {"name": "Optics & Photonics",
     "description": "Optical components, lasers, photonic & fiber networking.",
     "symbols": ["COHR", "LITE", "FN", "AAOI", "POET", "LWLG", "MKSI", "GLW", "NVMI", "OLED"]},
    {"name": "Data Centers",
     "description": "Data-center REITs, power/cooling, networking & neoclouds.",
     "symbols": ["DLR", "EQIX", "VRT", "SMCI", "ANET", "CRWV", "APLD", "NBIS", "IREN", "CIEN"]},
    {"name": "Software",
     "description": "Application & infrastructure software / SaaS.",
     "symbols": ["CRM", "NOW", "PLTR", "SNOW", "DDOG", "CRWD", "ORCL", "ADBE", "PANW", "NET", "MDB"]},
    {"name": "Defense",
     "description": "Defense primes plus drones, satellites & space exposure.",
     "symbols": ["LMT", "RTX", "NOC", "GD", "LHX", "KTOS", "AVAV", "LDOS", "RKLB", "ASTS"]},
    {"name": "Space",
     "description": "Launch, satellites, space infrastructure & comms.",
     "symbols": ["RKLB", "LUNR", "ASTS", "PL", "RDW", "BKSY", "SATS", "GSAT", "IRDM"]},
    {"name": "AI Biotech",
     "description": "AI/ML-driven drug discovery & techbio.",
     "symbols": ["RXRX", "SDGR", "TEM", "ABSI", "ABCL", "DNA", "CRSP", "NTLA"]},
    {"name": "Quantum Computing",
     "description": "Quantum computing hardware, software & post-quantum security.",
     "symbols": ["IONQ", "RGTI", "QBTS", "QUBT", "ARQQ", "LAES", "QMCO"]},
    {"name": "Nuclear",
     "description": "Nuclear power, SMRs, uranium miners & fuel.",
     "symbols": ["CCJ", "LEU", "OKLO", "SMR", "NNE", "BWXT", "CEG", "VST", "UEC", "NXE"]},
    {"name": "Rare Earths & Critical Minerals",
     "description": "Rare-earth & critical-mineral miners and processors.",
     "symbols": ["MP", "USAR", "TMC", "UUUU", "CRML", "NB", "UAMY", "ALB"]},
    {"name": "Apparel",
     "description": "Apparel, footwear & accessories brands.",
     "symbols": ["NKE", "LULU", "DECK", "ONON", "BIRK", "SKX", "CROX", "RL", "TPR", "ANF"]},
]


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
    seed_builtin_themes()


def list_themes():
    """[{id, name, description, builtin, symbols:[...]}] ordered by name."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, description, builtin FROM themes ORDER BY name"
        ).fetchall()
        syms = conn.execute(
            "SELECT theme_id, symbol FROM theme_symbols ORDER BY symbol"
        ).fetchall()
    by_theme = {}
    for tid, sym in syms:
        by_theme.setdefault(tid, []).append(sym)
    return [
        {"id": tid, "name": name, "description": desc or "",
         "builtin": bool(builtin), "symbols": by_theme.get(tid, [])}
        for tid, name, desc, builtin in rows
    ]


def get_theme(theme_id):
    for t in list_themes():
        if t["id"] == theme_id:
            return t
    return None


def save_theme(name, symbols, description="", theme_id=None):
    """Create (theme_id=None) or update; symbols replace wholesale. Returns id."""
    name = (name or "").strip()
    if not name:
        raise ValueError("theme name required")
    symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    with connect() as conn:
        if theme_id is None:
            cur = conn.execute(
                "INSERT INTO themes (name, description, builtin, created_at, updated_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (name, description, _now(), _now()),
            )
            theme_id = cur.lastrowid
        else:
            conn.execute(
                "UPDATE themes SET name=?, description=?, updated_at=? WHERE id=?",
                (name, description, _now(), theme_id),
            )
            conn.execute("DELETE FROM theme_symbols WHERE theme_id=?", (theme_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO theme_symbols (theme_id, symbol, added_at) "
            "VALUES (?, ?, ?)",
            [(theme_id, s, _now()) for s in symbols],
        )
    return theme_id


def delete_theme(theme_id):
    with connect() as conn:
        conn.execute("DELETE FROM themes WHERE id=?", (theme_id,))
        conn.execute("DELETE FROM theme_symbols WHERE theme_id=?", (theme_id,))


def seed_builtin_themes():
    """Insert any built-in theme whose name doesn't already exist (idempotent)."""
    with connect() as conn:
        for spec in BUILTIN_THEMES:
            exists = conn.execute(
                "SELECT 1 FROM themes WHERE name=?", (spec["name"],)
            ).fetchone()
            if exists:
                continue
            cur = conn.execute(
                "INSERT INTO themes (name, description, builtin, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (spec["name"], spec["description"], _now(), _now()),
            )
            tid = cur.lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO theme_symbols (theme_id, symbol, added_at) "
                "VALUES (?, ?, ?)",
                [(tid, s, _now()) for s in spec["symbols"]],
            )
