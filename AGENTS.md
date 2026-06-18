# AGENTS.md — Market Intelligence Harness

Operating guide for AI coding agents (ChatGPT Codex, Claude Code, etc.) working in this
repo. It is the short, actionable contract; **[CLAUDE.md](CLAUDE.md) is the canonical,
exhaustive architecture document** — read it before changing math or module wiring. Each
module also has its own `modules/<name>/README.md`.

> If you change behavior, keep all three in sync: this file (operational), `CLAUDE.md`
> (architecture), and the affected `modules/<name>/README.md` + the top-level
> [CHANGELOG.md](CHANGELOG.md).

---

## What this is

A personal, local-first market-intelligence platform. A vanilla Python stdlib HTTP server
(`app.py`) serves a set of self-contained modules, each with its own HTML page, that fetch
free market data (Yahoo Finance via `yfinance`, FRED, SEC, RSS) and turn it into rotation
calls, breadth/regime reads, screens, alerts, and a synthesis "harness" brief. The
end-goal module is the **harness**: every data module casts a signed vote, a deterministic
combiner decides a stance + score, and an LLM only *narrates* it. **Math decides, the LLM
explains** — never let the LLM override the deterministic decision.

No cloud, no build pipeline, no framework. Educational/personal use only.

---

## Setup & commands

**Use system Python — `/usr/bin/python3`.** The committed `.venv/` is intentionally empty;
do not rely on it. Dependencies (`yfinance pandas numpy requests`) are installed against
system Python.

```bash
# Install deps (one time)
pip3 install yfinance pandas numpy requests

# Run the server (http://localhost:8000)
/usr/bin/python3 app.py            # or: python3 app.py
# PORT=9000 python3 app.py         # override port

# Run the full test suite (stdlib unittest, no network — fixtures/mocks)
/usr/bin/python3 -m unittest discover tests

# Run one test module
/usr/bin/python3 -m unittest tests.test_harness

# Module CLIs (each is a standalone entry point)
python3 -m modules.breadth.cli [universe] [--json]
python3 -m modules.news.cli [--days N] [--high] [--json]
python3 -m modules.harness.cli [--llm] [--json] [--backtest] [--picks] [--paper] [--chat "q"]
```

There is **no linter/formatter config and no build step**. Match the surrounding style.

### Configuration (`.env`)

Copy `.env-example` → `.env`. Everything runs with **zero keys** (RRG + most reads use
Yahoo Finance, no key). Keys are optional and each *progressively enriches* one module:

- `SCHWAB_CLIENT_ID` / `SCHWAB_CLIENT_SECRET` / `SCHWAB_URI` — Schwab account + market data
  (OAuth tokens are written back into `.env` automatically; never edit those by hand).
- `FRED_API_KEY` — exact econ-release dates + Actual/Previous + the Rates & Curve tab + macro.
- `FINNHUB_API_KEY` — broad earnings calendar + EPS.
- `ALPHAVANTAGE_API_KEY` / `POLYGON_IO_KEY` — news-feed sentiment + ticker tagging.
- `DISCORD_WEBHOOK_URL` + `SMTP_*` / `ALERT_EMAIL_TO` — external alert delivery.

`.env` and all `modules/*/data/*.db` SQLite stores are **gitignored** — never commit them.

---

## Architecture

`app.py` is a `ThreadingHTTPServer` + a tiny `(method, path) → handler` router. It owns **no
business logic** — it imports each module's `register_routes(router)` and calls it. Threading
is required: long-running background syncs/pollers must not block the dashboard.

```
app.py                 # server + router only
modules/
  __init__.py          # shared Response class (Response.json / .html / .error)
  home/                # hub homepage + live status badges
  rrg/                 # RRG sector-rotation math + chart + backtester (the core signal)
  schwab/              # Schwab OAuth + positions enriched with RRG calls
  breadth/             # breadth indicators + regime + SQLite store + sync daemon
  screener/            # market screener + watchlists + alerts + backtester + pollers
  rankings/            # 0-99 sector RS leaderboard
  themes/              # editable theme baskets → reuse rankings/RRG engines
  confluence/          # ROUTELESS pure-leaf factor library (bottom of dep order)
  flow/                # options flow (unusual options activity) + poller
  canslim/             # CANSLIM 7-factor growth scorecard (pure composition)
  news/                # econ/earnings calendar + news feed + rates & curve
  macro/               # growth×inflation regime + "signals of health" panels
  harness/             # TOP consumer: votes → combiner → Claude-narrated brief + paper trading
  research/            # per-ticker fundamental conviction + sector/theme primers
tests/                 # stdlib unittest (no network)
```

### The module contract

Each module exposes exactly one function:

```python
from modules import Response

def handler(req):                       # req.path, req.qs (dict), req.headers, req.json_body()
    return Response.json({"k": "v"})    # or Response.html(s) / Response.error(msg, status)

def register_routes(router):
    router.get("/path", handler)
    router.post("/path", handler)
```

**Adding a module:** create `modules/<name>/__init__.py` with logic + `register_routes`,
drop `modules/<name>/<name>.html`, then add two lines in `app.py` (import + register).

### Dependency order — do not create cycles

The clean import order is `confluence (bottom) → breadth → schwab → rrg → … → harness/research (top)`.
Rules that keep it acyclic:

- **`confluence/` is a routeless pure-leaf library** (numpy/pandas only, no I/O, no upward
  imports). `rrg.signal`, `screener.metrics`/`snapshot`, and `flow.scoring` import *down*
  into it. The fetch/orchestration that feeds a leaf its prices stays in the **consumer**.
- A module reaching **up** the order (e.g. `rrg`/`screener` reading `breadth` regime, `news`
  reading `screener.store`) must do it via **lazy, in-function, fail-soft imports** — never a
  module-load import — so a missing/erroring dependency degrades gracefully instead of
  raising. `harness` and `research` are the **top consumers** (nothing imports them back);
  every cross-module reach in `harness/votes.py` and `research/__init__.py` is lazy + fail-soft.
- The handful of *intentional* hard cross-imports (schwab←rrg, breadth←schwab OAuth,
  screener←breadth bars, rankings←rrg/screener, themes←rrg/rankings) are enumerated in
  CLAUDE.md → "Development Conventions". Don't add new hard edges.

---

## The LLM layer

*(Important if you touch the harness / research / chat.)*

The app's own LLM calls go through the **local `claude` CLI subprocess** on the user's
**subscription** — *not* the metered Anthropic API. There is no `ANTHROPIC_API_KEY` and no
per-token cost.

- `modules/harness/agents.py::claude_cli()` shells out to `claude -p --output-format json
  --model <m>` with the prompt on **stdin**, parses `result`, and **fails soft to `None`**
  (missing binary / timeout / not logged in) → callers fall back to deterministic templates.
- Models are env-overridable: `HARNESS_MASTER_MODEL` (default Opus 4.8, alias `opus`),
  `HARNESS_DOMAIN_MODEL` (default Haiku 4.5, alias `haiku`). `HARNESS_LLM=0` forces the free
  deterministic path; `HARNESS_DOMAIN_LLM=1` narrates each domain with Haiku.
- The **combiner (`harness/combiner.py`) is deterministic and LLM-free** — that is what makes
  it replayable/backtestable by the referee. Keep decisions in the combiner; keep prose in
  `agents.py`. Do not move decision logic into an LLM call.

> Swapping the host agent to **ChatGPT Codex changes nothing about this** — the app still
> shells out to `claude`. If you (the agent) want the harness LLM features to render while
> working, the local `claude` binary must be logged in; otherwise everything renders
> deterministically (which is the intended `$0`/offline default). For new AI applications in
> this repo, default to the latest Claude models (Opus 4.8 / Haiku 4.5).

---

## Conventions

- **Free data first** — Yahoo Finance / FRED / SEC / RSS before any paid API. Every keyed
  source has a keyless fallback and is fail-soft.
- **Business logic in `modules/<name>/__init__.py`; `app.py` stays thin.** No new web
  framework — vanilla stdlib server + vanilla JS in the HTML. Add a framework only if UI
  complexity truly demands it (it doesn't yet).
- **Palette:** every page is white/navy (`--bg #fff`, `--panel #f4f7fc`, `--ink #0e2148`,
  accent `#16336e`, green `#1a9d6b`, red `#d1453b`, blue `#2f6fb3`). **RRG is intentionally
  the original dark theme** — leave it. When re-theming, note that Plotly layouts and
  tint-backed badge classes hold literal hex *outside* `:root`.
- **SQLite stores** (`breadth`, `screener`, `themes`, `flow`, `news`, `harness`): WAL mode,
  per-call connections (background threads + request threads coexist), `INSERT OR IGNORE` /
  `ON CONFLICT … DO UPDATE` for idempotent dedupe. Bars are owned by breadth and **read**,
  never duplicated, by screener.
- **No-lookahead is sacred** in any signal/backtest code. Levels are `shift(1)`'d; weekly
  signals confirm at week-end; the RRG ZigZag uses a forward-only fold. If you touch
  `rrg/signal.py` or any backtester, preserve this and run the golden-master test.
- **Fail-soft UI:** pages compose independent fetches so one slow/missing module degrades a
  single panel, never the whole page. Keep that pattern.

---

## Testing

- Tests are stdlib `unittest`, **hermetic (no network)** — sources are faked, the `claude`
  subprocess is mocked, DBs are temp files, prices/decisions are injected. Keep new tests
  network-free.
- `rrg`'s wave-engine extraction is pinned by a **golden-master** test
  (`tests/test_rrg_golden.py`) that asserts byte-identical `compute_rrg` output on a synthetic
  panel. A pure refactor must not change it; regenerate the fixture only when behavior is
  *meant* to change.
- Run `/usr/bin/python3 -m unittest discover tests` before finishing a change. There is one
  test file per module concern (`tests/test_<area>.py`).

---

## Gotchas

- Use `/usr/bin/python3`, not the empty `.venv`.
- `yfinance` is free but unofficial — transient empty pulls happen; code already retries/fails
  soft, and tests must not hit the network.
- `time.monotonic()` starts near 0 per process on macOS — never use `0.0` as a "never
  fetched" sentinel (use `None`). This bit the screener poller once.
- Pollers/sync jobs are singleton daemons gated to market hours; they auto-start in
  `register_routes` but are no-ops outside their window (and the paper daemon is a no-op in
  `manual` mode — autonomous trading is opt-in).
- **Real broker order placement is out of scope.** Schwab is read-only; the harness paper
  engine is simulated. Don't wire live order placement without an explicit, per-order
  confirmation request from the user.

---

## Where to read more

- **[CLAUDE.md](CLAUDE.md)** — full architecture, RRG math parameters, every module's design
  decisions and honest caveats. The source of truth.
- **[README.md](README.md)** — user-facing overview + per-module README links.
- **[CHANGELOG.md](CHANGELOG.md)** — dated history of what landed.
- **`modules/<name>/README.md`** — per-module deep dives.
