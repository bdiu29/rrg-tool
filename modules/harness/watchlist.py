"""
Watchlist import — turn an uploaded TradingView export into a normalized ticker
list (the trading focus). TradingView exports take a few shapes, all handled:

  • a watchlist **.txt**: comma-separated `EXCHANGE:SYMBOL` tokens with `###Section`
    headers — e.g. `NASDAQ:AAPL,NYSE:BRK.B,###Crypto,BINANCE:BTCUSDT`
  • a **CSV** with a `Ticker`/`Symbol` header column (the screener export)
  • a plain comma/newline list of symbols

We strip the `EXCHANGE:` prefix, drop `###` section headers, keep ticker-shaped
tokens, translate class-share dots to the yfinance convention (`BRK.B` → `BRK-B`),
and dedupe preserving order. Off-universe names are fine — the picks engine fetches
them on demand.
"""

import csv
import io
import re

from modules.harness import store

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_HEADER_KEYS = {"ticker", "symbol", "symbols"}


def _normalize(tok):
    """A raw token → a yfinance ticker, or None if it isn't ticker-shaped."""
    tok = (tok or "").strip().strip('"').strip("'").upper()
    if not tok or tok.startswith("#"):           # drop ###Section headers / blanks
        return None
    if ":" in tok:                               # EXCHANGE:SYMBOL → SYMBOL
        tok = tok.split(":")[-1].strip()
    if not _TICKER_RE.match(tok):
        return None
    return tok.replace(".", "-")                 # BRK.B → BRK-B (yfinance class shares)


def _ticker_column(rows):
    """If the first row is a header naming a ticker/symbol column, return its index."""
    if not rows:
        return None
    header = [c.strip().strip('"').lower() for c in rows[0]]
    for key in ("ticker", "symbol", "symbols"):
        if key in header:
            return header.index(key)
    return None


def parse_watchlist(text):
    """Uploaded text → ordered, deduped list of yfinance tickers (fail-soft → [])."""
    text = text or ""
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except Exception:
        rows = [[line] for line in text.splitlines()]
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return []

    col = _ticker_column(rows)
    if col is not None:
        tokens = [(r[col] if col < len(r) else "") for r in rows[1:]]
    else:
        tokens = [cell for r in rows for cell in r]   # flatten every cell

    out, seen = [], set()
    for tok in tokens:
        sym = _normalize(tok)
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def import_text(text, source="upload", replace=True):
    """Parse + persist. Returns {count, symbols, parsed}."""
    syms = parse_watchlist(text)
    count = store.set_watchlist(syms, source=source, replace=replace) if syms else \
        len(store.get_watchlist())
    return {"count": count, "parsed": len(syms), "symbols": store.get_watchlist()}
