"""
Schwab module — account positions with RRG-derived sector signals.

Routes registered:
  GET  /schwab.html           → schwab.html
  GET  /api/schwab/status     → OAuth connection status
  GET  /api/schwab/positions  → positions enriched with sector signals
  POST /api/schwab/exchange   → exchange OAuth code for tokens
"""

import base64
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode

try:
    import requests as _req
    _REQ_OK = True
except ImportError:
    _req    = None
    _REQ_OK = False

try:
    import yfinance as yf
except ImportError:
    yf = None

from modules import Response
from modules.rrg import compute_rrg, BENCHMARK, DEFAULT_TICKERS

_MODULE_DIR = Path(__file__).resolve().parent
_ROOT       = _MODULE_DIR.parent.parent   # rrg-tool/ — where .env lives

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHWAB_AUTH_URL  = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
SCHWAB_TRADER    = "https://api.schwabapi.com/trader/v1"

# yfinance sector string → SPDR sector ETF
SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Energy":                 "XLE",
    "Healthcare":             "XLV",
    "Health Care":            "XLV",
    "Financial Services":     "XLF",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Communication Services": "XLC",
}

CALL_TO_ACTION = {
    "ROTATE IN":  "BUY",
    "HOLD":       "HOLD",
    "ROTATE OUT": "SELL",
    "AVOID":      "AVOID",
    "WATCH":      "WATCH",
}

# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def _read_env():
    env      = {}
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _write_env_keys(updates):
    env_path = _ROOT / ".env"
    text     = env_path.read_text() if env_path.exists() else ""
    # Guarantee a trailing newline so appended keys land on their own line.
    if text and not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)
    for key, value in updates.items():
        for i, line in enumerate(lines):
            if line.split("=")[0].strip() == key:
                lines[i] = f"{key}={value}\n"
                break
        else:
            lines.append(f"{key}={value}\n")
    env_path.write_text("".join(lines))


def _clear_tokens():
    """Remove all Schwab OAuth tokens from .env (triggers re-auth on next load)."""
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    token_keys = {"SCHWAB_ACCESS_TOKEN", "SCHWAB_REFRESH_TOKEN", "SCHWAB_TOKEN_EXPIRY"}
    lines      = env_path.read_text().splitlines(keepends=True)
    lines      = [l for l in lines if l.split("=")[0].strip() not in token_keys]
    env_path.write_text("".join(lines))

# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _auth_url():
    env = _read_env()
    return SCHWAB_AUTH_URL + "?" + urlencode({
        "client_id":     env.get("SCHWAB_CLIENT_ID", ""),
        "redirect_uri":  env.get("SCHWAB_URI", "https://127.0.0.1"),
        "response_type": "code",
    })


def _exchange_code(code):
    env   = _read_env()
    creds = base64.b64encode(
        f"{env['SCHWAB_CLIENT_ID']}:{env['SCHWAB_CLIENT_SECRET']}".encode()
    ).decode()
    r = _req.post(
        SCHWAB_TOKEN_URL,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type":   "authorization_code",
              "code":          code,
              "redirect_uri":  env.get("SCHWAB_URI", "https://127.0.0.1")},
        timeout=15,
    )
    r.raise_for_status()
    d      = r.json()
    expiry = int(time.time()) + d.get("expires_in", 1800) - 60
    _write_env_keys({
        "SCHWAB_ACCESS_TOKEN":  d["access_token"],
        "SCHWAB_REFRESH_TOKEN": d["refresh_token"],
        "SCHWAB_TOKEN_EXPIRY":  str(expiry),
    })


def _valid_token():
    """Return a live access token, refreshing via the refresh token if needed."""
    env    = _read_env()
    token  = env.get("SCHWAB_ACCESS_TOKEN", "")
    expiry = int(env.get("SCHWAB_TOKEN_EXPIRY", "0"))
    if token and time.time() < expiry:
        return token

    rt = env.get("SCHWAB_REFRESH_TOKEN", "")
    if not rt:
        raise ValueError("Not authenticated — complete the Schwab connect flow.")

    creds = base64.b64encode(
        f"{env['SCHWAB_CLIENT_ID']}:{env['SCHWAB_CLIENT_SECRET']}".encode()
    ).decode()
    r = _req.post(
        SCHWAB_TOKEN_URL,
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": rt},
        timeout=15,
    )
    r.raise_for_status()
    d      = r.json()
    expiry = int(time.time()) + d.get("expires_in", 1800) - 60
    _write_env_keys({
        "SCHWAB_ACCESS_TOKEN":  d["access_token"],
        "SCHWAB_REFRESH_TOKEN": d.get("refresh_token", rt),
        "SCHWAB_TOKEN_EXPIRY":  str(expiry),
    })
    return d["access_token"]


def get_access_token():
    """Public token accessor for other modules (e.g. breadth market data).

    Returns a live access token, auto-refreshing if expired. Raises ValueError
    if the OAuth flow has never been completed.
    """
    return _valid_token()

# ---------------------------------------------------------------------------
# Sector lookup
# ---------------------------------------------------------------------------

_sector_cache = {}


def _sector_etf(symbol):
    """Map a stock symbol to its SPDR sector ETF via yfinance, with caching."""
    if symbol in DEFAULT_TICKERS:
        return symbol
    if symbol in _sector_cache:
        return _sector_cache[symbol]
    try:
        info = yf.Ticker(symbol).info
        etf  = SECTOR_ETF_MAP.get(info.get("sector", ""))
    except Exception:
        etf = None
    _sector_cache[symbol] = etf
    return etf

# ---------------------------------------------------------------------------
# Positions + signal enrichment
# ---------------------------------------------------------------------------

def _positions_with_signals():
    token = _valid_token()
    r = _req.get(
        f"{SCHWAB_TRADER}/accounts",
        params={"fields": "positions"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    r.raise_for_status()

    raw_positions    = []
    balances_by_acct = {}
    for acct in r.json():
        sec = acct.get("securitiesAccount", {})
        num = sec.get("accountNumber", "")
        bal = sec.get("currentBalances", {})
        balances_by_acct[num] = {
            "liquidation_value": bal.get("liquidationValue") or bal.get("cashBalance", 0),
            "cash":              bal.get("cashAvailableForTrading") or bal.get("cashBalance", 0),
        }
        for pos in sec.get("positions", []):
            inst = pos.get("instrument", {})
            sym  = inst.get("symbol", "")
            if not sym:
                continue
            raw_positions.append({
                "symbol":        sym,
                "description":   inst.get("description", sym),
                "asset_type":    inst.get("assetType", ""),
                "quantity":      pos.get("longQuantity", 0) - pos.get("shortQuantity", 0),
                "average_price": round(pos.get("averagePrice", 0), 4),
                "market_value":  round(pos.get("marketValue", 0), 2),
                "day_pnl":       round(pos.get("currentDayProfitLoss", 0), 2),
                "day_pnl_pct":   round(pos.get("currentDayProfitLossPercentage", 0), 3),
                "open_pnl":      round(pos.get("longOpenProfitLoss", 0), 2),
                "account":       num,
            })

    # Resolve sector ETFs for equity and ETF positions
    needed_etfs = set()
    sym_etf     = {}
    for pos in raw_positions:
        if pos["asset_type"] in ("EQUITY", "ETF"):
            etf = _sector_etf(pos["symbol"])
            sym_etf[pos["symbol"]] = etf
            if etf:
                needed_etfs.add(etf)
        else:
            sym_etf[pos["symbol"]] = None

    # Compute RRG signals for each relevant sector ETF
    rrg_signals = {}
    if needed_etfs:
        rrg = compute_rrg(list(needed_etfs), BENCHMARK, "1d")
        for etf, d in rrg["sectors"].items():
            rrg_signals[etf] = {
                "call":          d["call"],
                "call_why":      d["call_why"],
                "quadrant":      d["quadrant"],
                "heading_arrow": d["heading_arrow"],
            }

    enriched = []
    for pos in raw_positions:
        etf    = sym_etf.get(pos["symbol"])
        sig    = rrg_signals.get(etf, {}) if etf else {}
        call   = sig.get("call", "WATCH") if sig else "—"
        action = CALL_TO_ACTION.get(call, "—")
        enriched.append({
            **pos,
            "sector_etf":   etf,
            "rrg_call":     call,
            "rrg_why":      sig.get("call_why", ""),
            "rrg_quadrant": sig.get("quadrant", ""),
            "rrg_heading":  sig.get("heading_arrow", "·"),
            "action":       action,
        })

    return {
        "positions": enriched,
        "balances":  balances_by_acct,
        "updated":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_page(req):
    with open(_MODULE_DIR / "schwab.html") as f:
        return Response.html(f.read())


def _handle_status(req):
    env       = _read_env()
    has_creds = bool(env.get("SCHWAB_CLIENT_ID") and env.get("SCHWAB_CLIENT_SECRET"))
    token     = env.get("SCHWAB_ACCESS_TOKEN", "")
    expiry    = int(env.get("SCHWAB_TOKEN_EXPIRY", "0"))
    has_rt    = bool(env.get("SCHWAB_REFRESH_TOKEN", ""))
    return Response.json({
        "has_credentials": has_creds,
        "has_token":       bool(token),
        "token_expired":   bool(token and time.time() >= expiry),
        "has_refresh":     has_rt,
        "auth_url":        _auth_url() if has_creds else "",
    })


def _handle_positions(req):
    if not _REQ_OK:
        return Response.error("requests library not available in venv")
    try:
        return Response.json(_positions_with_signals())
    except ValueError as e:
        return Response.error(str(e), 401)
    except Exception as e:
        code = 500
        resp = getattr(e, "response", None)
        if resp is not None and getattr(resp, "status_code", 0) == 401:
            code = 401
        return Response.error(str(e), code)


def _handle_disconnect(req):
    _clear_tokens()
    return Response.json({"ok": True})


def _handle_exchange(req):
    if not _REQ_OK:
        return Response.error("requests library not available in venv")
    try:
        body   = req.json_body()
        code   = body.get("code", "")
        cb_url = body.get("callback_url", "")
        if cb_url:
            qs   = parse_qs(urlparse(cb_url).query)
            code = qs.get("code", [""])[0]
        if not code:
            return Response.error("No authorization code found in URL", 400)
        _exchange_code(code)
        return Response.json({"ok": True})
    except Exception as e:
        return Response.error(str(e))


def register_routes(router):
    router.get("/schwab.html",             _handle_page)
    router.get("/api/schwab/status",       _handle_status)
    router.get("/api/schwab/positions",    _handle_positions)
    router.post("/api/schwab/exchange",    _handle_exchange)
    router.post("/api/schwab/disconnect",  _handle_disconnect)
