# RRG — Sector Rotation

[← Back to main README](../../README.md)

Interactive Relative Rotation Graph for the 11 SPDR Select Sector ETFs, benchmarked against SPY. Plots where each sector sits in the rotation cycle and — more importantly — its *direction of travel*, then turns that into explicit ROTATE IN / ROTATE OUT / HOLD / AVOID / WATCH calls.

<p align="center">
  <img src="../../assets/chart.png" alt="RRG sector rotation chart" width="800"/>
</p>

Open at **http://localhost:8000/rrg.html**. Works with no `.env` — it only needs Yahoo Finance, which requires no key.

---

## What it shows

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

Sectors generally rotate **clockwise**: Improving → Leading → Weakening → Lagging → back to Improving. The fading "tail" behind each dot shows the recent path so you can see direction of travel.

### Reading the chart

Each sector is one dot (latest point, solid) with a tail of hollow dots showing where it's been:

- **Right half** (RS-Ratio > 100) = outperforming SPY; **left half** = underperforming.
- **Top half** (RS-Momentum > 100) = that performance is improving; **bottom** = deteriorating.

Tail direction and length are the primary read. A sector heading northeast with a long tail is a more committed move than one in the same quadrant with a stubby tail. Catching a sector as it moves from Lagging into Improving is the classic "early" entry signal.

---

## Controls and features

- **Timeframe toggle**: Weekly (big-picture rotation) vs Daily (the default — a macro lens in trading days, updated intraday).
- **Tail slider**: how many recent points to draw per sector (3–12).
- **As-of rollback**: step the chart back to a past date to see how the RRG looked then. All windows are trailing, so rollback only removes head points — historical tail points stay stable.
- **Copy buttons**: each chart has a "Copy" button that puts a PNG on your clipboard (paste into Slack, docs, etc.). Falls back to downloading the PNG if your browser blocks clipboard image writes.
- **Rotation Calls** (bottom panel): each sector gets an explicit call derived from its *direction of travel* and momentum, not just which quadrant it sits in. Split into a "Rotate In" column (ranked by accumulation/early-upside score) and a "Rotate Out" column (ranked by distribution/rolling-over score). A **Copy** button puts a plain-text snapshot on your clipboard.
- **Best Setup banner** (top): the single highest accumulation score — a sector turning up early (rising momentum, room below SPY), with quadrant, direction, and score.
- **vs SPY bar chart** (bottom-right): each sector's latest return relative to SPY, sorted best to worst.
- **Click a sector** in the list to hide/show it; the chart reframes to fit whatever's visible. **Hover a dot** for exact values.

<p align="center">
  <img src="../../assets/rotation-calls.png" alt="vs SPY bar chart and rotation calls" width="800"/>
</p>

### The logic behind each call

| Signal | Call |
|---|---|
| Improving + heading NE | ROTATE IN — momentum has turned, RS catching up |
| Lagging but turning up | ROTATE IN — earliest signal; momentum leads RS |
| Leading and still rising | HOLD |
| Leading rolling over / entering Weakening | ROTATE OUT — trim into strength |
| Lagging and still falling | AVOID |
| Stubby tail (little relative movement) | WATCH — not enough conviction to act |

A leg only counts as an impulse that can trigger a fresh ROTATE if it is *directional enough* (net travel over path length above a threshold) and large enough — bent, overlapping tails read as corrective and never trigger a fresh ROTATE. This is the whipsaw guard. Momentum sitting below its tail peak while RS holds its high reads as exhaustion → ROTATE OUT in Leading.

---

## How the math works

The chain is JdK-style on both intervals: **RS-Ratio** is built from the fast/slow EMA ratio of relative strength (a trend measure — x travels with the trend), and **RS-Momentum** is the rate-of-change of that trend (y is the velocity of x). Momentum being the derivative of the plotted ratio is what makes tails arc diagonally/clockwise like a real RRG.

- **Signal-space vs display-space.** The math (in `modules/rrg/signal.py`) keeps two coordinate systems that share the boundary at 100. The **signal** coords drive every call — quadrant, phase, gates — and are σ-normalized about a *fixed* center (RS-Ratio = 100, where the fast EMA equals the slow; momentum = 0), so the units mean the same thing on daily and weekly. The **display** coords sent to the chart are just a cosmetic gain on the signal coords with the offset pinned to exactly 100, so the dots you see and the calls you act on never disagree about which side of the line a sector is on. (This replaced an older daily affine map + weekly z-stretch that had been fitted to a reference image — the cosmetics had leaked into the calls, even nudging the quadrant boundary off the true line.)
- **Daily spans 50/140 days** (`mom_diff=15`, light `mom_smooth=3`, σ-windows 140d/60d); weekly spans fast 10w / slow 40w (`mom_diff=5`, σ-windows 52w/26w). The 40-week slow leg keeps the macro character — a 3-week pullback won't flip x, a real trend change will. Smoothing is deliberately light so momentum *leads* the ratio and leaders arc over the top of the oval; heavy smoothing lags and produces straight diagonal tails with no rollover.
- **Elliott-wave phase model (both intervals):** counter-trend momentum is squashed at the boundary so wave-2/4 pullbacks and wave-B bounces approach the quadrant line but don't cross it. Only a genuine trend flip (a new wave 1) releases the cap, so quadrant crossings reflect real motive→corrective alternation rather than laggard head-fakes. Each sector exports a `phase` field shown in the tooltip and feeding the call logic (a bounce becomes WATCH not ROTATE IN; a pullback becomes HOLD not ROTATE OUT).
- One tail point per calendar week, anchored to each week's **last** bar so points stay put as dates advance.

### Scoring

- **Accumulation score** (`_accum()`): a transparent heuristic in σ-units — `50 + 16·momentum-slope + 8·recent-kick + 5·room-below-SPY`, clamped 0–100. It ranks the "best" pick and the Rotate In column; it does *not* decide the binary call.
- **Distribution score** (`_distrib()`): the symmetric mirror, flagging leadership that's rolling over.
- **Tail heading** drives the calls — net direction of travel across the *whole* tail (snapped to an 8-point compass), not a single noisy bar. Core mental model: **momentum turns first, RS turns second.**

The axes are pinned to a fixed 90–116 / 94–106 frame for day-to-day consistency; they expand outward only if a sector would otherwise fall off-chart.

---

## Backtesting the calls

A **Strategy Backtest** tab on the page (and `POST /api/rrg/backtest`) replays the exact live call logic over ~3 years and asks the honest question: when the tool says ROTATE IN, does that sector actually beat SPY next?

- **Forward-return table by call type** — mean / excess-vs-SPY / win-rate at +1/5/10/20 days, measured from the first bar *after* the signal confirms (no lookahead). A working tool shows ROTATE IN with positive excess and ROTATE OUT negative.
- **Equity curve** — long the called sectors (exit on the opposing call, or a hold / ATR model), equal-weight, marked daily against SPY, with a trade-return histogram.
- **Walk-forward** — re-tunes the gate thresholds with expanding time folds and shows in-sample vs out-of-sample side by side, so overfit on a small sample (11 ETFs) is visible rather than hidden. The shipped defaults were baked from this search; re-run it to recalibrate against forward returns.

This is the replacement for the old "make the chart match a reference image" calibration — the calls are now tuned to forward returns, not to a picture.

---

## Configuration

Math and parameters in `modules/rrg/signal.py`; routes in `modules/rrg/__init__.py`:

| What | Where |
|---|---|
| Tickers / benchmark | `DEFAULT_TICKERS`, `BENCHMARK` (`signal.py`) |
| Per-sector colors | `SECTOR_COLORS` in `modules/rrg/index.html` |
| Cosmetic chart spread | `DISP_GAIN_X` / `DISP_GAIN_Y` (`signal.py`) — display only, no effect on calls |
| Gate thresholds (σ-units) | `signal.DEFAULTS` — baked from `backtest.walk_forward_search` |
| Call decision logic | `_rotation_call()` (`signal.py`) |
| Accumulation / distribution heuristics | `_accum()` / `_distrib()` (`signal.py`) |

> Educational only — confirm with price trend, not the RRG alone. This is a faithful, readable take on the JdK method rather than StockCharts' exact proprietary formula; the rotation *behavior* is what matters, not matching an exact value.
