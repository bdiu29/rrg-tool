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
- **JdK-style chain (both intervals):** `RS-Ratio` is built from the fast/slow EMA ratio of RS (a trend measure — x travels with the trend) and `RS-Momentum = ROC of that trend` (y is the velocity of x). Momentum being the derivative of the plotted ratio is what makes tails arc diagonally/clockwise like a real RRG — don't normalize the two axes independently.
- **Normalization differs by interval — this matters.** Weekly: per-ticker z-scores stretched by `RATIO_SCALE_WK=3.5` / `MOM_SCALE_WK=2.2`. Daily: **DIRECT affine scaling** of the raw trend ratio and its ROC (`x = RATIO_C_D + RATIO_K_D·(r−100)` = 100.55 + 1.678·(r−100); `y = 100 + MOM_K_D·m_adj`, K=1.883) — *no z-scores*. Per-ticker z-normalization divides each sector by its own σ, equalizing tail travel; the reference (sp-rrg.png, an Elliott-wave-based RRG, 2026-06-09) shows differential travel (XLE sweeps ~6 x-units while XLU crawls ~1), which only direct scaling preserves. Constants fitted by `calibrate_rrg.py` (staged grid search against digitized reference paths; mean head error ≈ 0.78) — rerun that script to recalibrate.
- **Elliott-wave phase model (daily momentum):** counter-trend momentum is squashed at the 100 line — `t_soft = tanh(Δ60d ratio / TREND_ETA)`; where `t_soft·mom < 0` (corrective leg: wave-2/4 pullback or wave-B bounce), `m_adj` is tanh-capped at `MOM_TAU` (≈0.2 y-units after K). Corrections approach the quadrant boundary but don't cross; only a genuine trend flip (t_soft ≈ 0 → sign change = new wave 1) releases the cap, so quadrant crossings reflect motive→corrective alternation. Verified: XLE's May-2026 wave-B bounce (+3.5 y-units raw) is suppressed instead of reading as rotation. Each sector exports a `phase` field ("impulse ↑ (wave 3/5)" / "pullback (wave 2/4)" / "impulse ↓ (wave C/3)" / "bounce (wave B)" / "basing/topping (trend turn)") shown in the tooltip and feeding `_rotation_call` (bounce → WATCH not ROTATE IN; pullback → HOLD not ROTATE OUT).
- Weekly spans: fast EMA 10w / slow EMA 40w, `mom_diff=5` (5-week ROC), z-windows 52w (ratio) / 26w (momentum). The 40-week slow leg keeps the macro character — a 3-week wave-2 pullback won't flip x, a real trend change will.
- **Daily = a macro lens in trading days** (50/140d spans — fast leg matches weekly's 10w, slow leg fitted slightly shorter than weekly's 40w; `mom_diff=15`, `mom_smooth=3`): a macro view updated intraday, NOT a faster story — short daily windows read 3-week laggard bounces as rotation (wave-2 head-fakes). `mom_smooth` is deliberately LIGHT: momentum must **lead** the ratio for leaders to arc over the top of the RRG oval (heavy smoothing = lag = straight diagonal tails, no rollover — this was the XLK-rollover bug). One tail point per calendar week, anchored to each week's **last** bar (not counted back from the newest bar) so points stay put as dates advance.
- **Intraday debug timeframes (1H / 2H):** same 50/140 trading-day lens with windows converted to bars (×6.5 / ×3.25 bars per day) — heads agree with Daily within ~1 unit; useful for watching the head develop intraday, not a different story. yfinance 60m data only goes back ~730d (2H is resampled from 60m). One tail point per trading day. Toggle buttons in the UI next to Weekly/Daily.
- Weekly z-score normalization uses a **flat** rolling window (not EWMA) — intentional; EWMA hugs its own recent values and collapses spread
- **As-of rollback:** `compute_rrg(..., asof="YYYY-MM-DD")` / `GET /api/rrg?asof=` truncates history to show the RRG as of a past date. All windows are trailing, so rollback only removes head points — historical tail points are stable. Downloads are cached in-memory for 10 min (`_PRICE_CACHE`) so date-stepping doesn't re-hit yfinance. Note: weekly bars are Monday-dated and contain the full week, so weekly rollback has whole-week granularity.

Tunable scoring functions in `modules/rrg/__init__.py`:
- `_accum()` — accumulation/rotate-in score (`50 + 6·mom_slope + 3·kick + 2·room`)
- `_distrib()` — distribution/rotate-out score (symmetric mirror of `_accum`)
- `_rotation_call()` — decision logic: ROTATE IN / ROTATE OUT / HOLD / AVOID / WATCH. Reads the tail in Elliott-wave terms: a leg only counts as impulse (wave 3/5, can trigger ROTATE) if `directness = net/path ≥ DIRECT_GATE` and `net ≥ MOVE_GATE`; bent/overlapping tails are corrective (wave 2/4) and never trigger a fresh ROTATE — the whipsaw guard. Momentum `DIV_MARGIN` below its tail peak while RS holds its high = wave-5 exhaustion → ROTATE OUT in Leading.

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
