# RRG — Local Sector Rotation Tool

An interactive Relative Rotation Graph (RRG) that runs entirely on your Mac.
A tiny Python server fetches price data and serves the chart at `localhost`.

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

Sectors generally rotate **clockwise**: Improving → Leading → Weakening → Lagging → back to Improving.
The fading "tail" behind each dot shows the recent path so you can see direction.

## One-time setup

You need Python 3 (macOS ships with it). Install the three dependencies:

```bash
pip3 install yfinance pandas numpy
```

## Run it

```bash
cd rrg-tool
python3 server.py
```

Then open **http://localhost:8000** in your browser.

Press `Ctrl+C` in the terminal to stop.

## Using it

- **Timeframe toggle**: Weekly (big-picture rotation) vs Daily (shorter-term).
- **Tail slider**: how many recent points to draw per sector (3–12, default 4).
- **Copy buttons**: each chart has a "Copy" button that puts a PNG on your
  clipboard (paste into Slack, docs, etc.). If your browser blocks clipboard
  images, it falls back to downloading the PNG.
- **Rotation Signals** (right panel): the actionable bit — any sector that
  *changed quadrant* on the latest bar is flagged with its transition (green ↗
  for rotating into Improving/Leading, red ↘ for rotating into
  Weakening/Lagging). When nothing crossed, it lists the strongest accumulation
  setups to watch instead.
- **Best Setup banner** (top): the single highest accumulation score — a sector
  turning up early (rising momentum, room below SPY), with quadrant, recent
  direction, and score.
- **vs SPY bar chart** (bottom): each sector's latest return relative to SPY,
  sorted best to worst.
- **Quadrant guide** (right panel): plain-English meaning of each quadrant.
- **Click a sector** in the list to hide/show it; the chart reframes to fit
  whatever's visible. **Hover a dot** for exact values.

## Reading the chart

Each sector is one dot (latest point, solid) with a tail of hollow dots showing
where it's been. Position is everything:
- **right half** (RS-Ratio > 100) = outperforming SPY; **left half** = lagging.
- **top half** (RS-Momentum > 100) = that performance is improving; **bottom** =
  deteriorating.
Sectors tend to travel **clockwise**: Improving → Leading → Weakening → Lagging.
Catching one as it moves from Lagging into Improving is the "early" signal.

## Tickers

The 11 SPDR Select Sector ETFs (XLK, XLE, XLV, XLF, XLY, XLP, XLI, XLB, XLU,
XLRE, XLC), benchmarked against SPY. To change them, edit `DEFAULT_TICKERS` and
`BENCHMARK` near the top of `server.py`. Per-sector colors live in
`SECTOR_COLORS` inside `index.html`.

## Notes

- Data comes from Yahoo Finance via `yfinance` — free, no API key, but it's an
  unofficial source so the occasional hiccup is normal. Just reload.
- Recent bars are weighted more via **EMA smoothing** of the relative-strength
  and momentum series (that's the right place for it — it speeds up the head
  without distorting the scale). The normalization itself uses a stable flat
  window, then the values are stretched by `RATIO_SCALE` / `MOM_SCALE` so the
  cloud fills the chart the way commercial RRGs do. It's a faithful, readable
  take on the JdK method rather than StockCharts' exact proprietary formula, so
  absolute values may differ — the rotation behavior is what matters.
- The axes are pinned to a fixed 90–116 / 94–106 frame (increments of 2) for
  day-to-day consistency; they expand outward only if a sector would otherwise
  fall off-chart. If the cloud looks too small or too large in the frame, adjust
  `RATIO_SCALE` / `MOM_SCALE` near the top of `server.py`.
- The **accumulation score** is a simple, transparent heuristic
  (`50 + 6·momentum-slope + 3·recent-kick + 2·room-below-SPY`, clamped 0–100).
  It's tunable in `_accum()` in `server.py` — it won't match any particular
  commercial tool's number, but the selection logic favors early rotation.