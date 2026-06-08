# Market Intelligence Harness — CLAUDE.md

## Project Vision

Personal market intelligence platform to guide investment/trading decisions: avoid drawdowns, catch rotation setups, and surface actionable signals. Built module by module from free data sources, culminating in AI subagents per data domain that feed a unified harness.

**Current state:** RRG tool (sector rotation) — the first analytics module.

**Planned modules:** sector/industry trackers (software, space, semis), Schwab account agent, news/macro events, JPM collar tracking, stock price data, fed interest rate probabilities.

---

## Architecture

Single-file per module for now — no framework, no build step.

| File | Role |
|---|---|
| `server.py` | Python HTTP server + all RRG math (JdK RS-Ratio, RS-Momentum, rotation calls) |
| `index.html` | Self-contained frontend — chart rendering, UI state, all JS inline |
| `.venv/` | Python virtualenv (Python 3.9) |

Data source: **Yahoo Finance via `yfinance`** — free, no API key required. Occasional hiccups are normal; reload to recover.

---

## Running the Tool

```bash
source .venv/bin/activate
python3 server.py
# → http://localhost:8000
```

Dependencies (already installed in `.venv`):
```
yfinance pandas numpy
```

---

## RRG Math — Key Parameters

Defined near the top of `server.py`:

- `DEFAULT_TICKERS` — 11 SPDR Select Sector ETFs (XLK, XLE, … XLC)
- `BENCHMARK` — `"SPY"` (all RS computed relative to this)
- `RATIO_SCALE = 3.0` / `MOM_SCALE = 1.8` — stretch normalized z-scores so the cloud fills the chart frame; increase to spread sectors further apart
- EMA smoothing (`span=5`) happens on the RS and momentum series before z-score normalization — weights recent bars more without collapsing the scale
- Z-score normalization uses a **flat** rolling window (not EWMA) — intentional; EWMA means hugs its own recent values and collapses spread

Tunable scoring functions in `server.py`:
- `_accum()` — accumulation/rotate-in score (`50 + 6·mom_slope + 3·kick + 2·room`)
- `_distrib()` — distribution/rotate-out score (symmetric mirror of `_accum`)
- `_rotation_call()` — decision logic: ROTATE IN / ROTATE OUT / HOLD / AVOID / WATCH

---

## Development Conventions

- Keep each module self-contained (one server file + one HTML file) until there's a clear reason to share code.
- Prefer free data sources first (yfinance, FRED, etc.) before paid APIs.
- No build pipeline — vanilla Python stdlib server, vanilla JS in HTML. Add a framework only when the UI complexity genuinely requires it.
- When adding a new data domain, model it after the RRG pattern: a `/api/<domain>` endpoint in the server, a dedicated frontend panel or page.

---

## Planned Expansion Path

1. **Sector/Industry Trackers** — RRG-style views for software, space, semiconductor sub-industries
2. **Schwab Account Agent** — portfolio positions, P&L, order management via Schwab API
3. **News & Macro Events** — headline feed, economic calendar, FOMC dates
4. **JPM Collar Tracker** — track the quarterly JPM collar strikes and expiry
5. **Fed Rate Probabilities** — CME FedWatch or similar for rate cut/hike odds
6. **AI Subagents** — one Claude subagent per data module, unified by a master harness agent

---

## Notes from Development

- The axes are pinned to a fixed frame (90–116 ratio, 94–106 momentum) and expand only if a sector would fall off-chart — keeps the visual stable day-to-day.
- Tail direction and tail length are the primary signal; quadrant location alone is insufficient. A short/stubby tail → WATCH regardless of quadrant.
- "Momentum turns first, RS turns second" — the core mental model driving entry/exit logic.
