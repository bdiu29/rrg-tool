# Changelog

All notable changes to the Market Intelligence Harness are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Dates are when the work landed; this project doesn't use version numbers yet, so
entries are grouped by date with an **Unreleased** section for work in progress.

## [Unreleased]

### Added
- **Breadth Tape** — a Stockbee-style **Market Monitor** as a second tab on the breadth page (`/breadth.html`): a dense daily table (newest day on top) of raw breadth counts — stocks up/down 4% today, 5/10-day ratios, up/down 25%+ over a quarter, up/down 25%+ & 50%+ over a month, up/down 13%+ over 34 days, a 10× ATR extension count, % above the 50-day MA, the stock-universe count, and the S&P (^GSPC) close — each cell green/red heat-colored, with Advancing/Declining and New-High/New-Low gauge bars and a **Copy as image** button (clipboard PNG). Scope toggle (All NYSE+Nasdaq default / S&P 500 / NYSE / Nasdaq); computed on demand from stored bars and reused across the existing breadth math. The dashboard chart now shows an honest error if Plotly's CDN fails to load instead of a perpetual "loading…".
- **Screenshot capture script** (`scripts/capture_screenshots.sh`) — Safari + macOS `screencapture` (no extra deps) to snapshot every page into `assets/screenshots/`, wired into the READMEs.
- **Themes module** (`/themes.html`) — track your own **theme baskets**. Each theme is a curated list of stocks turned into an equal-weight index, scored **0-99 vs SPY**, plotted on a **theme RRG** rotation chart with ROTATE calls, alongside daily/weekly rank movers and a per-theme **constituent drill-down**. Create / rename / delete themes and edit their tickers right in the page (SQLite-backed). Six themes seeded: Optics & Photonics, Data Centers, Software, Defense, Space, AI Biotech. Hub card + status badge.
- **Rankings module** (`/rankings.html`) — a relative-strength **leaderboard** for the 11 SPDR sectors: a **0-99 pooled-historical-percentile rank** vs SPY with its value 1D / 1W / 1M ago, RS% and 52-week-high columns, four **rank-mover** cards (daily/weekly up & down), and a top-stocks-per-sector drill-down with a **Relative Strength ↔ real Top Holdings** toggle. Hub card + status badge.
- READMEs for the Rankings and Themes modules; this `CHANGELOG.md`.

### Changed
- **App-wide white/navy theme.** Re-themed Home, Breadth, Schwab, Screener, Rankings, and Themes to a light white/navy palette (an earlier warm-parchment pass was superseded). The RRG keeps its original dark chart by design.
- **Reusable RS/RRG engines.** `compute_series`, `compute_rrg`, and `compute_rankings` now accept an optional pre-built price panel (`close=`), so synthetic baskets (themes) flow through the exact same sector math — fully backward compatible.
- **Cross-navigation.** Every page now links to every other page; added `Rankings →` and `Themes →` to all nav headers and the hub.

## [2026-06-15]

### Added
- **Empirically-weighted, regime-aware flag patterns** — the RRG conviction engine now weights its bull/bear flag factor by the flag's *measured* edge (`win_rate − 0.5`), per-symbol where there's enough history else a basket default, and zeroes a flag that opposes the market regime (bear flags fail upward in bull markets). Detection core extracted to a shared `flags.py` leaf; `flag_backtest.py` gains a `--regime` conditioned study. Per-stock flag win-rates are background-precomputed (incremental, **~90-day per-symbol cache**) and surfaced next to each name on the **Rankings** and **Themes** pages.
- **Volume buyer/seller exhaustion** — a volume *climax* into a new high/low that closes weak/strong flags a topping/bottoming, added as a confluence factor to the RRG conviction engine (on each symbol's own price+volume, since the RS line has none) and as a screener field.
- **Flag + exhaustion screener fields** — filter by `flag` (bull/bear/none) and `exhaustion` (buyer/seller/none), with seeded **Bull Flag / Bear Flag / Selling Climax / Buying Climax** screens.
- **Backtester: toggleable universe + benchmark, regime split, and gate.** The RRG Strategy Backtest tab adds a **universe** toggle (11 sectors / ~40 sector+industry ETFs / ~34 de-correlated), a **benchmark** toggle (SPY cap-weight / RSP equal-weight), a **rotation-regime split** (RSP/SPY trend) on the forward-return study, and a **per-symbol contribution** breakdown (top-3/top-5 share — is the curve broad or carried by a few names?).
- **Rotation-regime gate** — conviction is suppressed when the market is in a concentration regime (RSP/SPY below its trend, where a rotation signal structurally can't win). It's no-lookahead, so it gates the backtest too: on the 40-ETF set it cut max drawdown roughly in half and made the strategy beat the exposure-matched benchmark. Validated out-of-sample — the ROTATE IN − ROTATE OUT separation holds up across walk-forward folds.

### Changed
- **⚠️ w5 extended is no longer a backtest exit.** The larger study showed it has *positive* forward excess (continuation, not exhaustion), so exiting a long on it was selling winners early; it stays a cautionary late-cycle badge / Schwab TRIM but no longer forces an exit.
- Fixed a string-date vs Timestamp index mismatch that had silently nulled the breadth-regime conditioning.

> Honest caveat surfaced by this work: the backtest's headline equity is concentration-driven (a banks theme + a few small-sample winners), but the *relative* ranking edge (ROTATE IN beats ROTATE OUT) is real and holds out-of-sample. Use the tool as a rotation **ranking**, gated to rotation-on regimes — not as a literal absolute-return strategy. Next step (planned): a long/short or top-N-minus-bottom-N rotation sim to monetize that relative edge.

## [2026-06-14]

### Added
- **RRG strategy backtester** — replays the live rotation calls over ~3 years: forward-return study by call type (no lookahead), an equal-weight equity curve vs SPY, and a walk-forward search that re-tunes the gate thresholds with in-sample / out-of-sample folds. Surfaced as a "Strategy Backtest" tab on the RRG page.

## [2026-06-12]

### Added
- **Screener strategy backtester** — take a screen's conditions as an entry signal and replay them over history (long-only, next-open fills, no lookahead). Reports win rate, expectancy, profit factor, forward returns at +1/5/10/20d vs SPY, and an equity curve, exportable to Markdown. Records per-trade signal features for a future ML ranking layer.

## [2026-06-11]

### Added
- **Screener module** — TradingView-style filters over the full market (~5,300 symbols), saved screens, watchlists, EMA-distance and **golden-pocket** filters, and intraday **pump/dump alerts** delivered in-app and optionally to Discord / email. Backed by a derived indicator snapshot rebuilt from synced bars.
- **Breadth module** — McClellan oscillator/summation, advance-decline lines, % above moving averages and short-term EMA thrust, plus a scored **regime** state and dated **divergence** flags, across swappable universes (S&P 500 / NYSE / Nasdaq) with a resumable SQLite sync.

### Changed
- Improved the RRG signal model and fixed the rotation calls.
- Updated module READMEs.

## [2026-06-08]

### Added
- **Schwab module** — live account positions enriched with RRG-derived **BUY / HOLD / SELL** signals per holding, via an OAuth URL-paste flow.
- Copy-to-clipboard (PNG / text) on the RRG rotation-calls widget.

### Changed
- Reorganized the project into self-contained `modules/<name>/` packages behind a thin router (`app.py`).

## [2026-06-06]

### Added
- **Initial release** — an interactive **Relative Rotation Graph** for the 11 SPDR Select Sector ETFs vs SPY: JdK-style RS-Ratio / RS-Momentum with a signal/display split, an Elliott-wave phase model, explicit ROTATE IN/OUT/HOLD/AVOID/WATCH calls, and a vs-SPY bar chart — served by a vanilla Python stdlib server with a no-build JS frontend.
- Project scaffolding: `CLAUDE.md`, `.env-example`, `.gitignore`, assets, and the top-level README.
