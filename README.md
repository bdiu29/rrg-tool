# Market Intelligence Harness — Local Market Analytics

A personal market intelligence platform that runs entirely on your Mac.
A tiny Python server fetches data and serves an interactive dashboard at `localhost` — no cloud, no subscriptions, no API fees for the core data.

**Current modules:**
- **Home** — hub page linking every module, with live status badges
- **RRG** — Interactive Relative Rotation Graph for sector ETFs
- **Breadth** — Market breadth tracker: McClellan, A-D lines, % above MAs, regime state & divergences across swappable universes (S&P 500 / NYSE / Nasdaq)
- **Schwab** — Account positions enriched with daily sector rotation signals

<p align="center">
  <img src="assets/chart.png" alt="RRG sector rotation chart" width="800"/>
</p>

---

## One-time setup

You need Python 3 (macOS ships with it). Install the dependencies:

```bash
pip3 install yfinance pandas numpy requests
```

**For the Schwab module**, create a `.env` file in the project root (copy `.env-example`):

```bash
cp .env-example .env
```

Then fill in your Schwab developer credentials. Get them from [developer.schwab.com](https://developer.schwab.com) — you'll need to create an app with `https://127.0.0.1` as the redirect URI.

```
SCHWAB_CLIENT_ID=your_app_key
SCHWAB_CLIENT_SECRET=your_app_secret
SCHWAB_URI=https://127.0.0.1
```

The RRG module works without a `.env` file — it only needs Yahoo Finance, which requires no key.

---

## Run

```bash
cd rrg-tool
python3 app.py
```

Then open **http://localhost:8000** in your browser.

| Page | URL |
|---|---|
| Home — Module Hub | http://localhost:8000/ |
| RRG — Sector Rotation | http://localhost:8000/rrg.html |
| Breadth — Market Breadth | http://localhost:8000/breadth.html |
| Schwab — Account Positions | http://localhost:8000/schwab.html |

Press `Ctrl+C` in the terminal to stop.

---

## RRG — Sector Rotation

### What it shows

Each sector ETF is plotted by:
- **X axis — JdK RS-Ratio**: relative strength vs the benchmark (SPY)
- **Y axis — JdK RS-Momentum**: the momentum of that relative strength

The four quadrants tell you where each sector sits in the rotation cycle:

```
        IMPROVING  │  LEADING
     (rising, weak)│(rising, strong)
     ──────────────┼──────────────
        LAGGING    │  WEAKENING
     (falling,weak)│(falling,strong)
```

Sectors generally rotate **clockwise**: Improving → Leading → Weakening → Lagging → back to Improving.
The fading "tail" behind each dot shows the recent path so you can see direction of travel.

### Controls and features

- **Timeframe toggle**: Weekly (big-picture rotation) vs Daily (shorter-term).
- **Tail slider**: how many recent points to draw per sector (3–12, default 4).
- **Schwab Account →** (top-left nav): jump to your positions page.
- **Copy buttons**: each chart has a "Copy" button that puts a PNG on your clipboard (paste into Slack, docs, etc.). Falls back to downloading the PNG if your browser blocks clipboard image writes.
- **Rotation Signals** (right panel): any sector that *changed quadrant* on the latest bar is flagged (green ↗ for rotating into Improving/Leading, red ↘ for rotating into Weakening/Lagging). When nothing crossed, it lists the strongest accumulation setups to watch instead.
- **Rotation Calls** (bottom panel): each sector gets an explicit call — **ROTATE IN / ROTATE OUT / HOLD / AVOID / WATCH** — derived from its *direction of travel* and momentum, not just which quadrant it sits in. The panel splits candidates into a "Rotate In" column (ranked by accumulation/early-upside score) and a "Rotate Out" column (ranked by distribution/rolling-over score). There's a **Copy** button on this panel that puts a plain-text snapshot on your clipboard — useful for pasting into notes or messages.
- **Best Setup banner** (top): the single highest accumulation score — a sector turning up early (rising momentum, room below SPY), with quadrant, direction, and score.
- **vs SPY bar chart** (bottom-right): each sector's latest return relative to SPY, sorted best to worst.
- **Quadrant guide** (right panel): plain-English meaning of each quadrant.
- **Click a sector** in the list to hide/show it; the chart reframes to fit whatever's visible. **Hover a dot** for exact values.

The logic behind each call:

| Signal | Call |
|---|---|
| Improving + heading NE | ROTATE IN — momentum has turned, RS catching up |
| Lagging but turning up | ROTATE IN — earliest signal; momentum leads RS |
| Leading and still rising | HOLD |
| Leading rolling over / entering Weakening | ROTATE OUT — trim into strength |
| Lagging and still falling | AVOID |
| Stubby tail (little relative movement) | WATCH — not enough conviction to act |

![vs SPY bar chart](assets/barchart.png)

### Reading the chart

Each sector is one dot (latest point, solid) with a tail of hollow dots showing where it's been:

- **Right half** (RS-Ratio > 100) = outperforming SPY; **left half** = underperforming.
- **Top half** (RS-Momentum > 100) = that performance is improving; **bottom** = deteriorating.

Tail direction and length are the primary read. A sector heading northeast with a long tail is a more committed move than one in the same quadrant with a stubby tail. Catching a sector as it moves from Lagging into Improving is the classic "early" entry signal.

### Tickers and configuration

The default universe is the 11 SPDR Select Sector ETFs (XLK, XLE, XLV, XLF, XLY, XLP, XLI, XLB, XLU, XLRE, XLC), benchmarked against SPY. To change them, edit `DEFAULT_TICKERS` and `BENCHMARK` near the top of `modules/rrg/__init__.py`. Per-sector colors live in `SECTOR_COLORS` inside `modules/rrg/index.html`.

If the cloud looks too small or too large in the chart frame, adjust `RATIO_SCALE` / `MOM_SCALE` in the same file.

---

## Breadth — Market Breadth Tracker

Answers one question continuously: **is the broad market participating in a move, or is it being carried by a handful of large stocks?** Designed for short-term S&P timing (SPY/ES), with long-term breadth as a regime layer that colors how the short-term signals are read — breadth is always shown against index price on a shared time axis, because breadth in isolation is close to useless.

<p align="center">
  <img src="assets/breadth.png" alt="Market breadth dashboard — regime banner, metrics, and stacked breadth panels against SPY" width="800"/>
</p>

### Regime banner

The top of the dashboard states the current regime, derived from the long-term panel:

| Regime | Meaning |
|---|---|
| **HEALTHY** | Summation Index positive *and* rising *and* >60% of stocks above their 200d MA — oversold readings are buy-the-dip setups, can size up |
| **NEUTRAL** | Mixed long-term panel — wait for confirmation |
| **DETERIORATING** | Narrow market (<50% above 200d), falling Summation, or active divergences — the same oversold readings get faded, smaller size, tighter stops |

**Divergence flags** are discrete dated events (not just lines): the index making a fresh ~quarterly high while the A-D line or % above 50d/20d MA fails to confirm raises a bearish flag (bullish mirror at lows). Active flags subtract from the regime score and are marked on the price panel.

### Panels (shared time axis)

1. **Index price** with divergence markers and Zweig Breadth Thrust events
2. **Cumulative A-D line** + A-D volume line
3. **McClellan Oscillator** (ratio-adjusted) + Summation Index
4. **% of stocks above 20/50/200-day MA**
5. **TRIN (Arms Index)** + up/down volume ratio
6. **52-week new highs − new lows** + High-Low Index
7. **Concentration gauge** — RSP/SPY ratio (equal-weight vs cap-weight; falling = top-heavy market)

### Universes

Swappable at runtime via the buttons in the header: **S&P 500** (Wikipedia constituents), **NYSE** and **Nasdaq** (nasdaqtrader.com listings, common stock only). Universes are defined in `modules/breadth/universes.json` — adding Russell 2000 or S&P 600 is a config entry (generic CSV fetcher included), no code changes.

**Survivorship caveat (important):** universes are built from *today's* constituent lists, so deep historical breadth is biased upward — delisted losers are missing. Recent readings are reliable; treat multi-year history as approximate. Membership is stored dated so point-in-time lists can be imported later.

### Data & backfill

- Prices come from the **Schwab Market Data API** by default (free with your existing developer app; the OAuth tokens from the Schwab module are reused), with **yfinance as automatic fallback** when Schwab isn't connected. Adapters are pluggable (`modules/breadth/datasource.py`).
- Everything lands in a local **SQLite store** (`modules/breadth/data/breadth.db`); indicators always compute from local data, never by re-pulling history.
- The first backfill per universe pulls 3 years of daily bars — ~5 min for the S&P 500, 20–40 min for NYSE/Nasdaq via Schwab (rate-limited to stay under 120 requests/min). It runs in the background with a progress bar, is **resumable** (interrupt and re-run — already-synced symbols are skipped), and subsequent updates are incremental.
- **Math note:** the McClellan Oscillator/Summation are *ratio-adjusted* (1000·(adv−dec)/(adv+dec)) so readings are comparable across a 500-stock and a 3,000-stock universe. The Bullish Percent Index is stubbed — it needs a point-&-figure signal engine.

### Daily summary CLI

```bash
python3 -m modules.breadth.cli            # S&P 500 summary
python3 -m modules.breadth.cli nasdaq     # another universe
python3 -m modules.breadth.cli --json     # machine-readable
```

Prints the current regime with reasons, short-term extremes interpreted *through* the regime, and any active divergence flags.

---

## Schwab — Account Positions

Connect your Schwab brokerage account to see your positions alongside daily RRG sector rotation signals. Each holding gets a **BUY / HOLD / SELL / WATCH / AVOID** flag derived from its sector ETF's current rotation signal.

### Connecting your account

1. Open **http://localhost:8000/schwab.html** in your browser.
2. Click **Open Schwab Login** — a new tab opens on Schwab's authorization page.
3. Log in and approve access. Schwab will redirect to `https://127.0.0.1`, which won't load — that's expected.
4. Copy the full URL from your browser's address bar (it contains `?code=…`).
5. Paste it into the field on the page and click **Connect Account**.

Your access token is saved to `.env` and refreshed automatically. Refresh tokens last 7 days; if yours expires, just reconnect.

### What you see

| Column | Description |
|---|---|
| Symbol / Description | The position's ticker and name |
| Qty | Number of shares held |
| Mkt Value | Current market value |
| Day P&L | Today's unrealized gain/loss for the position |
| Open P&L | Total unrealized gain/loss since purchase |
| Sector | The SPDR sector ETF the holding maps to |
| Signal | Sector's daily RRG heading arrow + quadrant |
| **Action** | **BUY / HOLD / SELL / WATCH / AVOID** |

Hover the Signal cell to read the full rationale behind the call. Non-equity positions (options, bonds, money market) show `—` for sector and signal — they don't map to a sector ETF.

### Buttons

- **Refresh** — re-fetch your positions and recalculate signals.
- **Export CSV** — download your current positions table as a `.csv` file (includes all columns).
- **Disconnect** — clears your saved tokens from `.env` and drops you back to the connect flow, so you can re-authenticate or switch accounts.

---

## Technical notes

- **Data** comes from Yahoo Finance via `yfinance` — free, no API key, but it's an unofficial source so the occasional hiccup is normal. Just reload.
- **Recent bars are weighted more** via EMA smoothing of the relative-strength and momentum series (that's the right place for it — it speeds up the head without distorting the scale). The normalization itself uses a stable flat window, then the values are stretched by `RATIO_SCALE` / `MOM_SCALE` so the cloud fills the chart the way commercial RRGs do. It's a faithful, readable take on the JdK method rather than StockCharts' exact proprietary formula — the rotation behavior is what matters, not matching an exact value.
- **The axes** are pinned to a fixed 90–116 / 94–106 frame for day-to-day consistency; they expand outward only if a sector would otherwise fall off-chart.
- **The accumulation score** is a transparent heuristic (`50 + 6·momentum-slope + 3·recent-kick + 2·room-below-SPY`, clamped 0–100), tunable in `_accum()` in `modules/rrg/__init__.py`. Its mirror, the **distribution score** (`_distrib()`), uses the symmetric form to flag leadership that's rolling over.
- **Tail direction and length** drive the calls. `_tail_heading()` measures the net direction of travel across the *whole* tail (snapped to an 8-point compass) rather than a single noisy bar. A move with too short a tail is treated as low-conviction (WATCH) regardless of quadrant — momentum turns first, RS turns second.
- **Sector lookup** for Schwab positions uses `yfinance` to map each stock to its GICS sector and then to the corresponding SPDR ETF. Results are cached in memory for the lifetime of the server process.
- **Architecture**: one `modules/<name>/` folder per data domain; `app.py` owns only the HTTP server and routing. Adding a new module is three steps: create the folder, add a `register_routes(router)` function, import it in `app.py`.
