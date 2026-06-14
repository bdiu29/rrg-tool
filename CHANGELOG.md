# Changelog

All notable changes to the Market Intelligence Harness are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Dates are when the work landed; this project doesn't use version numbers yet, so
entries are grouped by date with an **Unreleased** section for work in progress.

## [Unreleased]

### Added
- **Themes module** (`/themes.html`) — track your own **theme baskets**. Each theme is a curated list of stocks turned into an equal-weight index, scored **0-99 vs SPY**, plotted on a **theme RRG** rotation chart with ROTATE calls, alongside daily/weekly rank movers and a per-theme **constituent drill-down**. Create / rename / delete themes and edit their tickers right in the page (SQLite-backed). Six themes seeded: Optics & Photonics, Data Centers, Software, Defense, Space, AI Biotech. Hub card + status badge.
- **Rankings module** (`/rankings.html`) — a relative-strength **leaderboard** for the 11 SPDR sectors: a **0-99 pooled-historical-percentile rank** vs SPY with its value 1D / 1W / 1M ago, RS% and 52-week-high columns, four **rank-mover** cards (daily/weekly up & down), and a top-stocks-per-sector drill-down with a **Relative Strength ↔ real Top Holdings** toggle. Hub card + status badge.
- READMEs for the Rankings and Themes modules; this `CHANGELOG.md`.

### Changed
- **App-wide white/navy theme.** Re-themed Home, Breadth, Schwab, Screener, Rankings, and Themes to a light white/navy palette (an earlier warm-parchment pass was superseded). The RRG keeps its original dark chart by design.
- **Reusable RS/RRG engines.** `compute_series`, `compute_rrg`, and `compute_rankings` now accept an optional pre-built price panel (`close=`), so synthetic baskets (themes) flow through the exact same sector math — fully backward compatible.
- **Cross-navigation.** Every page now links to every other page; added `Rankings →` and `Themes →` to all nav headers and the hub.

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
