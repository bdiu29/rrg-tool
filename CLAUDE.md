# Market Intelligence Harness — CLAUDE.md

## Project Vision

Personal market intelligence platform to guide investment/trading decisions: avoid drawdowns, catch rotation setups, and surface actionable signals. Built module by module from free data sources, culminating in AI subagents per data domain that feed a unified harness.

**Current state:** RRG tool (sector rotation) + Schwab account module (positions with sector signals).

**Planned modules:** sector/industry trackers (software, space, semis), news/macro events, JPM collar tracking, fed interest rate probabilities, AI subagents.

---

## Architecture

Each module is a self-contained Python package in `modules/<name>/` with its own HTML frontend.
`app.py` is the single entry point — it owns the HTTP server and delegates routing to modules.

```
app.py                       # HTTP server + router (no business logic)
modules/
  __init__.py                # shared Response class
  rrg/
    __init__.py              # RRG math + route handlers + register_routes()
    index.html               # RRG frontend
  schwab/
    __init__.py              # Schwab OAuth + API + route handlers + register_routes()
    schwab.html              # Schwab frontend
.env                         # API keys + Schwab tokens (never committed)
.venv/                       # Python 3.9 virtualenv
```

### Module contract

Each module exposes one function:

```python
def register_routes(router):
    router.get("/path", handler_fn)
    router.post("/path", handler_fn)
```

Route handlers receive a `Request` object and return a `Response`:

```python
from modules import Response

def handler(req):
    # req.path, req.qs (dict), req.headers, req.json_body()
    return Response.json({"key": "value"})
    # or Response.html(html_str)
    # or Response.error("message", status=500)
```

### Adding a new module

1. Create `modules/<name>/__init__.py` with your logic and a `register_routes(router)` function.
2. Drop the frontend HTML at `modules/<name>/<name>.html`.
3. In `app.py`, add two lines:
   ```python
   from modules.<name> import register_routes as register_<name>
   register_<name>(router)
   ```

---

## Running the Tool

```bash
source .venv/bin/activate
python3 app.py
# → http://localhost:8000
```

Pages:
- `http://localhost:8000/` — RRG sector rotation chart
- `http://localhost:8000/schwab.html` — Schwab account positions + sector signals

Dependencies (already installed in `.venv`):
```
yfinance pandas numpy requests
```

---

## RRG Math — Key Parameters

Defined in `modules/rrg/__init__.py`:

- `DEFAULT_TICKERS` — 11 SPDR Select Sector ETFs (XLK, XLE, … XLC)
- `BENCHMARK` — `"SPY"` (all RS computed relative to this)
- `RATIO_SCALE = 3.0` / `MOM_SCALE = 1.8` — stretch normalized z-scores so the cloud fills the chart frame; increase to spread sectors further apart
- EMA smoothing (`span=5`) on RS/momentum series before z-score normalization — weights recent bars without collapsing scale
- Z-score normalization uses a **flat** rolling window (not EWMA) — intentional; EWMA hugs its own recent values and collapses spread

Tunable scoring functions in `modules/rrg/__init__.py`:
- `_accum()` — accumulation/rotate-in score (`50 + 6·mom_slope + 3·kick + 2·room`)
- `_distrib()` — distribution/rotate-out score (symmetric mirror of `_accum`)
- `_rotation_call()` — decision logic: ROTATE IN / ROTATE OUT / HOLD / AVOID / WATCH

---

## Schwab Module

- **OAuth flow:** URL-paste pattern — browser opens Schwab login, redirects to `https://127.0.0.1` (connection fails), user pastes the full URL back. Redirect URI registered: `https://127.0.0.1` (env var: `SCHWAB_URI`).
- **Token storage:** `SCHWAB_ACCESS_TOKEN`, `SCHWAB_REFRESH_TOKEN`, `SCHWAB_TOKEN_EXPIRY` in `.env` — auto-refreshed on every `/api/schwab/positions` call.
- **Sector mapping:** `yf.Ticker(symbol).info["sector"]` → `SECTOR_ETF_MAP` → SPDR ETF. Cached in-memory (`_sector_cache`). Only equities and ETFs get sector lookup.
- **Action flags:** ROTATE IN → BUY, HOLD → HOLD, ROTATE OUT → SELL, AVOID → AVOID, WATCH → WATCH.

---

## Development Conventions

- One `modules/<name>/` folder per data domain. No cross-module imports except: schwab imports `compute_rrg`, `BENCHMARK`, `DEFAULT_TICKERS` from rrg (intentional dependency — schwab signals are derived from RRG).
- Prefer free data sources first (yfinance, FRED, etc.) before paid APIs.
- No build pipeline — vanilla Python stdlib server, vanilla JS in HTML. Add a framework only when the UI complexity genuinely requires it.
- Business logic lives in module `__init__.py`. `app.py` stays thin.

---

## Planned Expansion Path

1. **Sector/Industry Trackers** — RRG-style views for software, space, semiconductor sub-industries
2. **News & Macro Events** — headline feed, economic calendar, FOMC dates
3. **JPM Collar Tracker** — track the quarterly JPM collar strikes and expiry
4. **Fed Rate Probabilities** — CME FedWatch or similar for rate cut/hike odds
5. **AI Subagents** — one Claude subagent per data module, unified by a master harness agent

---

## Notes from Development

- The axes are pinned to a fixed frame (90–116 ratio, 94–106 momentum) and expand only if a sector would fall off-chart — keeps the visual stable day-to-day.
- Tail direction and tail length are the primary signal; quadrant location alone is insufficient. A short/stubby tail → WATCH regardless of quadrant.
- "Momentum turns first, RS turns second" — the core mental model driving entry/exit logic.
