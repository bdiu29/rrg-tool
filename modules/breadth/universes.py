"""
Universe config + constituent fetchers (all free sources).

Universes are defined in universes.json — symbol lists and index symbols are
swappable without touching indicator code. Constituent lists fetched here are
TODAY'S members: deep historical breadth computed from them is survivorship-
biased (delisted losers are missing). Membership is stored dated in the
members table so point-in-time lists can be imported later.
"""

import csv
import io
import json
import re
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from . import store

_MODULE_DIR  = Path(__file__).resolve().parent
_CONFIG_PATH = _MODULE_DIR / "universes.json"

_UA = {"User-Agent": "Mozilla/5.0 (market-breadth-tracker; personal use)"}

# Security-name keywords that mark non-common-stock listings to exclude.
_EXCLUDE_NAME = re.compile(
    r"warrant|right(s)?\b|\bunit(s)?\b|preferred|depositary|%|due \d{4}|notes",
    re.IGNORECASE,
)


def load_config():
    with open(_CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fetchers — each returns a list of canonical (dot-form) symbols
# ---------------------------------------------------------------------------

def _fetch_wikipedia_sp500(_uni):
    r = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers=_UA, timeout=30,
    )
    r.raise_for_status()
    soup  = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", id="constituents") or soup.find("table", class_="wikitable")
    syms  = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if cells:
            sym = cells[0].get_text(strip=True).upper()
            if sym and re.fullmatch(r"[A-Z][A-Z.\-]{0,6}", sym):
                syms.append(sym)
    if len(syms) < 400:
        raise RuntimeError(f"Wikipedia S&P 500 scrape returned only {len(syms)} symbols")
    return sorted(set(syms))


def _parse_nasdaqtrader(url, keep_row):
    r = requests.get(url, headers=_UA, timeout=30)
    r.raise_for_status()
    lines  = r.text.splitlines()
    header = lines[0].split("|")
    syms   = []
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            break
        parts = line.split("|")
        if len(parts) != len(header):
            continue
        row = dict(zip(header, parts))
        if keep_row(row):
            syms.append(row.get("Symbol") or row.get("ACT Symbol"))
    return sorted({s for s in syms if s})


def _fetch_nasdaq(_uni):
    """Nasdaq Composite ≈ all Nasdaq-listed common stock (non-ETF, non-test)."""
    def keep(row):
        if row.get("Test Issue") != "N" or row.get("ETF") == "Y":
            return False
        name = row.get("Security Name", "")
        if _EXCLUDE_NAME.search(name):
            return False
        sym = row.get("Symbol", "")
        return bool(re.fullmatch(r"[A-Z]{1,5}", sym))
    return _parse_nasdaqtrader(
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", keep
    )


def _fetch_nyse(_uni):
    """NYSE Composite ≈ NYSE-listed (Exchange N) common stock."""
    def keep(row):
        if row.get("Exchange") != "N" or row.get("Test Issue") != "N" or row.get("ETF") == "Y":
            return False
        name = row.get("Security Name", "")
        if _EXCLUDE_NAME.search(name):
            return False
        sym = row.get("ACT Symbol", "")
        if "$" in sym:          # preferred share classes
            return False
        return bool(re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", sym))
    return _parse_nasdaqtrader(
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", keep
    )


def _fetch_csv_url(uni):
    """Generic CSV fetcher so new universes (Russell 2000, S&P 600) are pure
    config: needs 'url' and 'symbol_column' keys on the universe entry."""
    r = requests.get(uni["url"], headers=_UA, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    col    = uni["symbol_column"]
    syms   = [row[col].strip().upper() for row in reader if row.get(col, "").strip()]
    return sorted(set(syms))


_FETCHERS = {
    "wikipedia_sp500":   _fetch_wikipedia_sp500,
    "nasdaqtrader_nyse":  _fetch_nyse,
    "nasdaqtrader_nasdaq": _fetch_nasdaq,
    "csv_url":            _fetch_csv_url,
}


# ---------------------------------------------------------------------------
# Membership refresh
# ---------------------------------------------------------------------------

def refresh_constituents(universe_key):
    cfg = load_config()
    uni = cfg["universes"][universe_key]
    symbols = _FETCHERS[uni["source"]](uni)
    store.upsert_members(universe_key, symbols)
    return symbols


def get_constituents(universe_key, force_refresh=False):
    """Active members, refreshed from source when stale. Falls back to the
    cached list if the source is unreachable."""
    cfg     = load_config()
    max_age = cfg["settings"].get("constituents_max_age_days", 7)
    last    = store.get_meta(f"members_refreshed:{universe_key}")
    stale   = True
    if last:
        age   = (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days
        stale = age >= max_age
    if force_refresh or stale:
        try:
            return refresh_constituents(universe_key)
        except Exception:
            cached = store.get_members(universe_key)
            if cached:
                return cached
            raise
    return store.get_members(universe_key)
