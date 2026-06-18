# Market Intelligence Harness — Local Market Analytics

A personal market intelligence platform that runs entirely on your Mac. A tiny Python server fetches data and serves an interactive dashboard at `localhost` — no cloud, no subscriptions, no API fees for the core data. Built module by module, each a self-contained package with its own page, **culminating in an AI harness** that fuses every module's signal into one daily brief.

Suggested GitHub repo name: **`market-intelligence-harness`** (the project has grown well beyond the original RRG tool).

<p align="center">
  <img src="assets/chart.png" alt="RRG sector rotation chart" width="820"/>
</p>

---

## Modules

Each module has its own README with the full nuts-and-bolts deep dive. See [CHANGELOG.md](CHANGELOG.md) for the history of main updates, [CLAUDE.md](CLAUDE.md) for the exhaustive architecture, and [AGENTS.md](AGENTS.md) for the agent/contributor contract.

| Module | What it does | Deep dive |
|---|---|---|
| **Home** | Hub page linking every module, with live status badges | [modules/home](modules/home/README.md) |
| **RRG** | Interactive Relative Rotation Graph for sector ETFs — rotation calls per sector, plus a walk-forward-validated backtester | [modules/rrg](modules/rrg/README.md) |
| **Breadth** | Market breadth tracker: McClellan, A-D lines, % above MAs / short-term EMA thrust, regime & divergences across swappable universes (S&P 500 / NYSE / Nasdaq), plus a **Breadth Tape** tab (Stockbee-style Market Monitor) | [modules/breadth](modules/breadth/README.md) |
| **Schwab** | Account positions enriched with daily sector rotation signals (BUY / HOLD / SELL) | [modules/schwab](modules/schwab/README.md) |
| **Screener** | TradingView-style screener over the whole market (EMA, golden-pocket, flag, exhaustion, accumulation filters) + watchlists + intraday pump/dump alerts (Discord / email) + a **strategy backtester** | [modules/screener](modules/screener/README.md) |
| **Rankings** | Relative-strength leaderboard for the 11 SPDR sectors — 0-99 percentile ranks vs SPY, rank movers, and the strongest stocks / real top holdings per sector | [modules/rankings](modules/rankings/README.md) |
| **Themes** | Editable **theme baskets** turned into equal-weight indices, ranked 0-99 vs SPY with a theme RRG chart, movers, and constituent drill-down | [modules/themes](modules/themes/README.md) |
| **Confluence** | A routeless **shared factor library** of pure leaves — bull/bear flags, volume exhaustion, volume profile, accumulation/distribution, institutional sponsorship, and the wave engine — each emitting a signed contribution every module folds in | [modules/confluence](modules/confluence/README.md) |
| **Flow** | **Options flow** — unusual options activity (whale entries/exits) via a flow trader's 6-rule filter over Schwab option-chain snapshots, with per-contract factor drill-down and alerts | [modules/flow](modules/flow/README.md) |
| **CANSLIM** | O'Neil/IBD's **7-factor growth scorecard** (C-A-N-S-L-I-M) composed from breadth, rankings, accumulation, and screener fundamentals into a 0-99 per-stock leaderboard | [modules/canslim](modules/canslim/README.md) |
| **News** | **News & macro events** — a week-by-week economic + earnings calendar, a market news feed (RSS + SEC 8-K + sentiment), and a **Rates & Curve** tab, with an event-risk hook | [modules/news](modules/news/README.md) |
| **Macro** | **Growth×inflation regime** classifier (Goldilocks / Reflation / Stagflation / Disinflation as probabilities) + "signals of health" leading & macro indicator panels | [modules/macro](modules/macro/README.md) |
| **Harness** | **The synthesis layer + project end-goal** — every module casts a signed vote, a deterministic regime-arbitrated combiner decides a CONCENTRATE/ROTATE stance, and Claude narrates one daily brief. Plus watchlist trade suggestions + a paper-trading engine | [modules/harness](modules/harness/README.md) |
| **Research** | **Per-ticker fundamental conviction** read (0-99 + verdict over six sub-scores) that backs whether a name is solid enough to hold/size — wired into the harness pick HOLD axis; plus sector/theme primers | [modules/research](modules/research/README.md) |

Every page shares a light **white/navy** theme; the RRG keeps its original dark chart.

### RRG — direction of travel, not just position

The [RRG module](modules/rrg/README.md) plots each sector's relative strength vs SPY and turns its *direction of travel* into explicit ROTATE IN / OUT / HOLD / AVOID / WATCH calls, with a vs-SPY bar chart and a ranked rotation-calls panel.

<p align="center">
  <img src="assets/rotation-calls.png" alt="vs SPY bar chart and ranked rotation calls" width="820"/>
</p>

### Screener — scan, watch, get alerted

The [Screener module](modules/screener/README.md) filters ~5,300 symbols with savable screens, builds watchlists, and raises same-day pump/dump alerts on your positions and watchlist names — in-app and optionally to Discord/email. Filters include price-vs-EMA distances (5/10/20/50/100/200) and a **golden-pocket** scanner (price in / approaching the 0.618–0.786 Fibonacci retracement of the latest swing, both directions).

<p align="center">
  <img src="assets/screener.png" alt="Stock screener — filter builder, results table, watchlists, and alerts" width="900"/>
</p>

### Backtester — validate a confluence before you trade it

The screener doubles as a **strategy backtester**: take your current screen conditions as an entry signal and replay them over history (long-only, next-open fills, no lookahead). It reports win rate, expectancy, profit factor, forward returns at +1/5/10/20 days vs SPY, and an equity curve — exportable to Markdown. The RRG ships its own [rotation-call backtester](modules/rrg/README.md) with walk-forward validation, a long/short rotation portfolio, and a take-profit-ladder comparison.

<p align="center">
  <img src="assets/backtest.png" alt="Backtest report — stat grid, equity curve vs SPY, and forward-return study" width="900"/>
</p>

### Rankings & Themes — what's strongest, right now

The [Rankings module](modules/rankings/README.md) is a scannable leaderboard: each of the 11 SPDR sectors gets a **0-99 relative-strength rank** (a pooled-historical percentile of its strength vs SPY), shown next to its rank a day / week / month ago, with **rank movers** up and down and a top-stocks/top-holdings drill-down. The [Themes module](modules/themes/README.md) does the same for **custom baskets** you define (optics, data centers, software, defense, space, AI biotech are seeded — edit them in the page), each turned into an equal-weight index, scored 0-99, and plotted on a **theme RRG**.

### Macro & Harness — the regime read and the daily brief

The [Macro module](modules/macro/README.md) classifies the **growth×inflation regime** (Goldilocks / Reflation / Stagflation / Disinflation as probability bars + a plain-English playbook) and tracks "signals of health" — leading and macro indicator panels, each a value + 20-bar change + STABLE/COMPLACENT/WATCH/TURNED state + a plain-English meaning. The [Harness module](modules/harness/README.md) is the project's end-goal: it turns every data module into a signed **vote**, a **deterministic regime-arbitrated combiner** decides a CONCENTRATE-vs-ROTATE stance + composite score + confluence longs/avoids, and Claude **narrates** one daily brief — *math decides, the LLM explains*. It also takes a TradingView watchlist and surfaces impulse×hold-ability **trade suggestions** that feed a cost-modeled **paper-trading** book.

---

## One-time setup

You need Python 3 (macOS ships with it). Install the dependencies:

```bash
pip3 install yfinance pandas numpy requests
```

The RRG, Rankings, Themes, and Macro modules work with no configuration — they only need Yahoo Finance / FRED, which require no key (FRED is optional and enriches Macro/News).

Create a `.env` file in the project root by copying the example:

```bash
cp .env-example .env
```

Everything runs with **zero keys**. Each key below is *optional* and progressively enriches one module — see `.env-example` for the full annotated list.

**Schwab** (account positions + market data + options flow) — credentials from [developer.schwab.com](https://developer.schwab.com); create an app with `https://127.0.0.1` as the redirect URI:

```
SCHWAB_CLIENT_ID=your_app_key
SCHWAB_CLIENT_SECRET=your_app_secret
SCHWAB_URI=https://127.0.0.1
```

**Free data keys** (each instant + free-tier):

```
FRED_API_KEY=...          # exact econ-release dates + Actual/Previous + Rates & Curve tab + Macro
FINNHUB_API_KEY=...       # broad earnings calendar + EPS estimates/actuals
ALPHAVANTAGE_API_KEY=...  # news-feed sentiment + ticker tagging (~25 req/day)
POLYGON_IO_KEY=...        # news feed (Benzinga ticker news + sentiment); options tape is paid-only
```

**Alert delivery** (optional — the Screener and Flow modules can route alerts to Discord/email; see the [Screener README](modules/screener/README.md#delivery--channel-routing)):

```
DISCORD_WEBHOOK_URL=...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=you@gmail.com
SMTP_PASS=your_app_password
ALERT_EMAIL_TO=you@gmail.com
```

**Harness AI brief** (optional) — the Harness and Research modules narrate their output through the local **`claude` CLI** on your Claude subscription (no API key, no per-token cost). If `claude` isn't installed/logged in, every page renders deterministically — the brief just uses a plain-English template instead. See [AGENTS.md](AGENTS.md#the-llm-layer) for details.

---

## Run

```bash
cd market-intelligence-harness
/usr/bin/python3 app.py
```

Then open **http://localhost:8000**. Press `Ctrl+C` in the terminal to stop.

| Page | URL |
|---|---|
| Home — Module Hub | http://localhost:8000/ |
| RRG — Sector Rotation | http://localhost:8000/rrg.html |
| Breadth — Market Breadth (+ Breadth Tape tab) | http://localhost:8000/breadth.html |
| Schwab — Account Positions | http://localhost:8000/schwab.html |
| Screener — Stock Screener | http://localhost:8000/screener.html |
| Rankings — Sector Leaderboard | http://localhost:8000/rankings.html |
| Themes — Theme Tracker | http://localhost:8000/themes.html |
| Flow — Options Flow | http://localhost:8000/flow.html |
| CANSLIM — Growth Scorecard | http://localhost:8000/canslim.html |
| News — News & Macro Events | http://localhost:8000/news.html |
| Macro — Regime & Signals of Health | http://localhost:8000/macro.html |
| Harness — Market-Intelligence Dashboard | http://localhost:8000/harness.html |
| Research — Fundamental Researcher | http://localhost:8000/research.html |

> **Data note:** the core data comes from Yahoo Finance via `yfinance` (free, no key, but unofficial — the occasional hiccup is normal; just reload) and, where you've connected Schwab, the Schwab Market Data API. Everything is educational only — confirm with price trend, not these tools alone.

---

## Screenshots

Each UI module's README opens with a screenshot of its page (see the [Modules](#modules) table above). They live in [`assets/`](assets/); to (re)generate them on a Mac — no extra dependencies, uses Safari + the built-in `screencapture`:

```bash
/usr/bin/python3 app.py                 # in one terminal
bash scripts/capture_screenshots.sh     # in another
```

First run prompts for Screen Recording permission for your terminal. The script captures the Breadth Tape via `/breadth.html?view=tape` and the Schwab page via `/schwab.html?privacy=1`, so screenshots stay reproducible and do not expose account holdings.

---

## Architecture

One `modules/<name>/` folder per data domain; `app.py` owns only the HTTP server, routing, and static `/assets/...` serving (no business logic). Each module exposes a single `register_routes(router)` function and ships its own HTML frontend.

```
app.py                  # ThreadingHTTPServer + router + static /assets
assets/                 # shared app-shell.js + README screenshots
modules/
  __init__.py           # shared Response class
  home/                 # hub homepage + live status badges
  rrg/                  # RRG math + chart + rotation-call backtester
  breadth/              # breadth indicators + dashboard + SQLite store + sync daemon
  schwab/               # Schwab OAuth + positions
  screener/             # screener + watchlists + alerts + backtester + SQLite store
  rankings/             # sector RS leaderboard — 0-99 ranks, movers, holdings
  themes/               # editable theme baskets → RRG + ranking + SQLite store
  confluence/           # routeless pure-leaf factor library (bottom of the dep order)
  flow/                 # options flow + 6-rule filter + poller + SQLite store
  canslim/              # CANSLIM 7-factor scorecard (pure composition)
  news/                 # econ/earnings calendar + news feed + rates & curve + SQLite store
  macro/                # growth×inflation regime + signals-of-health panels
  harness/              # votes → combiner → Claude brief + paper trading (top consumer)
  research/             # per-ticker fundamental conviction + sector/theme primers
tests/                  # stdlib unittest — /usr/bin/python3 -m unittest discover tests
```

Modules sit in a clean dependency order — `confluence` (a routeless pure-leaf library) at the bottom, the `harness` and `research` (top consumers) at the top. Reaches *up* the order are lazy, in-function, and fail-soft, so a missing or erroring module degrades one panel rather than crashing the page. The intentional cross-imports are documented in [CLAUDE.md](CLAUDE.md). Adding a new module is three steps: create the folder, add a `register_routes(router)` function, import it in `app.py`.

Run the test suite with:

```bash
/usr/bin/python3 -m unittest discover tests
```
