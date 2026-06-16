"""
RRG strategy backtester — does the rotation call actually precede the move?

Replays the *exact* live call logic over history (`signal.replay_calls`) and
asks the honest question the deleted reference image never could: when the tool
says ROTATE IN, does that sector outperform SPY over the next 1/5/10/20 days,
and does ROTATE OUT underperform?

Two outputs:
  * an EVENT STUDY — forward returns (and excess vs SPY) grouped by call type,
    measured from the next bar after the signal is confirmed (no lookahead);
  * a TRADE SIM + equity curve — long the called sectors, exit on the opposing
    call (or a hold/ATR model), equal-weight, marked daily vs SPY.

`walk_forward_search` tunes the call-driving gate params with expanding-window
folds and reports in-sample vs out-of-sample separation, so overfit on a tiny
sample (11 ETFs) is visible rather than hidden. It is a guardrail, not a
fitting machine.

No screener import — RRG sits below screener in the dependency order, so the
small aggregation helpers are replicated here.
"""

import itertools
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from . import signal
from .signal import DEFAULT_TICKERS, BENCHMARK, DEFAULTS

FWD_HORIZONS = (1, 5, 10, 20)
SEP_HORIZON  = 10           # horizon the walk-forward objective separates on

# Backtest universes (toggleable). The default 11 SPDR sectors give too few onsets
# per conviction bucket to trust the confidence study; a broader liquid ETF set
# (≥3y history, no fundamentals/cross-module deps needed — RRG sits at the bottom
# of the dependency order) yields ~3–4× the onsets for a less noisy read. All are
# scored vs SPY exactly like the sectors.
_EXTENDED_ETFS = DEFAULT_TICKERS + [
    "SMH", "SOXX", "XBI", "IBB", "IGV", "FDN", "SKYY", "KWEB", "ARKK",
    "XOP", "OIH", "XME", "GDX", "XHB", "ITB", "XRT", "KRE", "KBE", "IYT",
    "JETS", "ITA", "XAR", "TAN", "ICLN", "LIT", "URA", "PAVE", "CIBR", "BOTZ",
]

# Near-duplicate, same-industry ETFs whose co-presence quietly doubles a bet (the
# 40-ETF run's top contribution was KRE+KBE = the *same* banks theme). The
# de-correlated set keeps one representative per industry so the backtest can't
# lean on a doubled position: drop SOXX (≈SMH semis), IBB (≈XBI biotech), SKYY
# (≈IGV software/cloud), XHB (≈ITB homebuilders), KBE (≈KRE banks), XAR (≈ITA A&D).
_DEDUP_DROP = {"SOXX", "IBB", "SKYY", "XHB", "KBE", "XAR"}
_DEDUP_ETFS = [t for t in _EXTENDED_ETFS if t not in _DEDUP_DROP]

UNIVERSES = {
    "sectors":    {"label": "11 SPDR sectors",       "tickers": DEFAULT_TICKERS},
    "etfs":       {"label": "Sector + industry ETFs", "tickers": _EXTENDED_ETFS},
    "etfs_dedup": {"label": "De-correlated ETFs",     "tickers": _DEDUP_ETFS},
}
DEFAULT_UNIVERSE = "sectors"


def _resolve_universe(name):
    """(universe_key, tickers). Unknown / falsy → the default sector set."""
    key = name if name in UNIVERSES else DEFAULT_UNIVERSE
    return key, UNIVERSES[key]["tickers"]


# Benchmark the forward excess + equity are scored against. The SIGNAL is always
# RS-vs-SPY (the calls never change) — this only swaps the yardstick. RSP (equal-
# weight S&P) is the fair bar for sector rotation: it strips the mega-cap-beta
# penalty that makes cap-weighted SPY nearly unbeatable in a concentration regime.
BENCHMARKS = {"SPY": "SPY (cap-weight)", "RSP": "RSP (equal-weight)"}
DEFAULT_BENCHMARK = "SPY"

def _bench(close, idx, symbol):
    """(series, array) for a benchmark column, reindexed + ffilled to idx."""
    s = close[symbol] if symbol in close.columns else pd.Series(dtype=float)
    arr = (s.reindex(idx).ffill().to_numpy() if not s.empty
           else np.full(len(idx), np.nan))
    return s, arr


def _rotation_series(close, idx):
    """Per-date rotation regime Series ('on'/'off') aligned to idx — equal-weight
    (RSP) vs cap-weight (SPY) above/below its trend. Reuses `signal._rotation_label`
    (one definition, trailing EMA → no-lookahead) so the live gate and the backtest
    agree. None (no split / no gate) if RSP wasn't downloaded — fail-soft."""
    if "RSP" not in close.columns or BENCHMARK not in close.columns:
        return None
    lab = signal._rotation_label(close["RSP"], close[BENCHMARK])
    if lab.empty:
        return None
    with pd.option_context("future.no_silent_downcasting", True):
        return lab.reindex(idx).ffill()
TRADE_CAP    = 400
# The two extension warnings are first-class calls (own event-study rows). Both
# are now "hold but tighten", NOT exits: the event study showed ⚠️ w5 extended has
# strongly POSITIVE forward excess (+1.3% at +10d) — it's continuation, not
# exhaustion — so exiting a long on it was selling winners early. It stays a
# cautionary late-cycle badge (schwab TRIM) but no longer forces a trade-sim exit
# nor counts on the bearish side of the walk-forward separation objective.
CALL_ORDER   = ["ROTATE IN", "ROTATE OUT", "⚠️ w5 extended", "⚠️ w3 extended",
                "HOLD", "WATCH", "AVOID"]
EXIT_CALLS   = ("ROTATE OUT", "AVOID")

DEFAULT_EXIT = {
    "model":      "signal",   # signal | hold | atr
    "hold_days":  10,
    "atr_stop":   2.0,
    "atr_target": 3.0,
    "max_hold":   40,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _num(x, places=4):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, places)


def _exit_cfg(raw):
    cfg = dict(DEFAULT_EXIT)
    if raw:
        cfg.update({k: raw[k] for k in raw if k in DEFAULT_EXIT})
    cfg["model"]      = cfg["model"] if cfg["model"] in ("signal", "hold", "atr") else "signal"
    cfg["hold_days"]  = max(1, int(cfg["hold_days"]))
    cfg["max_hold"]   = max(1, int(cfg["max_hold"]))
    cfg["atr_stop"]   = float(cfg["atr_stop"])
    cfg["atr_target"] = float(cfg["atr_target"])
    return cfg


def _entry_lag(interval):
    """Daily points are the week's last bar (signal known at its close) → enter
    the next bar. Weekly bars are Monday-dated but only confirmed at week's end,
    so shift to that week's Friday before taking the next bar (no lookahead)."""
    return pd.Timedelta(days=4) if interval == "1wk" else pd.Timedelta(0)


def _atr14(h, l, c):
    n = len(c)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        if np.isfinite(h[i]) and np.isfinite(l[i]) and np.isfinite(c[i - 1]):
            tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return pd.Series(tr).rolling(14, min_periods=14).mean().to_numpy()


def _onsets(timeline):
    """Ordered [(date, call, conviction)] keeping only the bars where the call
    *changed* (conviction taken at the onset bar)."""
    out, prev = [], None
    for d in sorted(timeline):
        call = timeline[d]["call"]
        if call != prev:
            out.append((d, call, timeline[d].get("conviction")))
            prev = call
    return out


# ---------------------------------------------------------------------------
# Event study (model-independent — the headline validation)
# ---------------------------------------------------------------------------

def _event_records(calls, close_df, bench_arr, idx, lag, regime=None):
    """One record per call onset: {call, date, fwd{h}, excess{h}, regime}, measured
    from the entry bar (first bar after the signal is confirmed). `bench_arr` is the
    chosen benchmark (SPY or RSP); `regime` (optional) tags each onset on/off."""
    n = len(idx)
    recs = []
    for tk, timeline in calls.items():
        if tk not in close_df.columns:
            continue
        col = close_df[tk].to_numpy()
        for d, call, conv in _onsets(timeline):
            ei = int(idx.searchsorted(d + lag, side="right"))
            if ei >= n:
                continue
            base, sbase = col[ei], bench_arr[ei]
            if not np.isfinite(base) or base <= 0:
                continue
            fwd, ex = {}, {}
            for h in FWD_HORIZONS:
                if ei + h < n and np.isfinite(col[ei + h]):
                    r = (col[ei + h] / base - 1) * 100
                    fwd[h] = r
                    if np.isfinite(sbase) and sbase > 0 and np.isfinite(bench_arr[ei + h]):
                        ex[h] = r - (bench_arr[ei + h] / sbase - 1) * 100
            recs.append({"call": call, "conviction": conv, "date": idx[ei],
                         "fwd": fwd, "excess": ex,
                         "regime": (regime[ei] if regime is not None else None)})
    return recs


def _regime_split_study(recs, calls=("ROTATE IN", "ROTATE OUT")):
    """Split the actionable calls' forward excess by the rotation regime at onset —
    the direct test of "does the signal work only when rotation is live?". None if
    no record carries a regime tag (RSP unavailable)."""
    if not any(r.get("regime") for r in recs):
        return None
    out = {"rows": []}
    for call in calls:
        for state, label in (("on", "rotation on"), ("off", "rotation off")):
            rows = [r for r in recs if r["call"] == call and r.get("regime") == state]
            horizons = {}
            for h in FWD_HORIZONS:
                ex = np.array([r["excess"][h] for r in rows if h in r["excess"]], dtype=float)
                horizons[str(h)] = {
                    "excess":   _num(ex.mean()) if ex.size else None,
                    "win_rate": _num(100.0 * (ex > 0).mean()) if ex.size else None,
                    "n":        int(ex.size),
                }
            out["rows"].append({"call": call, "regime": state, "label": label,
                                "n_onsets": len(rows), "horizons": horizons})
    return out


def _event_study(recs):
    by_call = defaultdict(list)
    for r in recs:
        by_call[r["call"]].append(r)
    out = {}
    for call in CALL_ORDER:
        rows = by_call.get(call, [])
        if not rows:
            continue
        horizons = {}
        for h in FWD_HORIZONS:
            fr = np.array([r["fwd"][h] for r in rows if h in r["fwd"]], dtype=float)
            ex = np.array([r["excess"][h] for r in rows if h in r["excess"]], dtype=float)
            if fr.size:
                horizons[str(h)] = {
                    "mean":     _num(fr.mean()),
                    "median":   _num(np.median(fr)),
                    "win_rate": _num(100.0 * (fr > 0).mean()),
                    "excess":   _num(ex.mean()) if ex.size else None,
                    "n":        int(fr.size),
                }
        out[call] = {"n_onsets": len(rows), "horizons": horizons}
    return out


def _confidence_study(recs, horizon=SEP_HORIZON):
    """The probabilistic model's honest test: bucket ROTATE IN / ROTATE OUT onsets
    by conviction and report forward excess per bucket. If higher conviction →
    higher forward excess (monotonic), the confluence score is doing real work."""
    buckets = [(0, 40), (40, 55), (55, 70), (70, 85), (85, 100)]
    out = {"horizon": horizon, "rows": []}
    for lo, hi in buckets:
        ex = [r["excess"][horizon] for r in recs
              if r["call"] == "ROTATE IN" and r.get("conviction") is not None
              and lo <= r["conviction"] < hi and horizon in r["excess"]]
        out["rows"].append({
            "bucket": f"{lo}–{hi}", "n": len(ex),
            "excess": _num(float(np.mean(ex))) if ex else None,
            "win_rate": _num(100.0 * np.mean(np.array(ex) > 0)) if ex else None,
        })
    # crude monotonicity read: excess of the top populated bucket vs the bottom
    vals = [(r["bucket"], r["excess"]) for r in out["rows"] if r["excess"] is not None]
    out["monotone_hint"] = (vals[-1][1] - vals[0][1]) if len(vals) >= 2 else None
    return out


def _separation(recs, lo, hi, horizon=SEP_HORIZON, min_n=3):
    """Mean excess of ROTATE IN minus mean excess of ROTATE OUT at `horizon`, over
    onsets in (lo, hi]. NaN if either side is too thin to trust. (⚠️ w5 extended is
    no longer on the bearish side — it proved to be continuation, not exhaustion.)"""
    out_calls = ("ROTATE OUT",)
    ins  = [r["excess"][horizon] for r in recs
            if r["call"] == "ROTATE IN"     and lo < r["date"] <= hi and horizon in r["excess"]]
    outs = [r["excess"][horizon] for r in recs
            if r["call"] in out_calls       and lo < r["date"] <= hi and horizon in r["excess"]]
    if len(ins) < min_n or len(outs) < min_n:
        return float("nan")
    return float(np.mean(ins) - np.mean(outs))


# ---------------------------------------------------------------------------
# Trade simulation + equity curve
# ---------------------------------------------------------------------------

def _exit(ei, entry_px, atr0, forced, forced_is_signal, cfg, o, h, l, c, n):
    model = cfg["model"]
    hard  = min(ei + cfg["max_hold"], n - 1)

    if model == "hold":
        xi = min(ei + cfg["hold_days"] - 1, hard)
        return xi, c[xi], ("hold" if xi == ei + cfg["hold_days"] - 1 else "eod")

    if model == "atr" and np.isfinite(atr0) and atr0 > 0:
        stop, target = entry_px - cfg["atr_stop"] * atr0, entry_px + cfg["atr_target"] * atr0
        cap = min(hard, forced)
        for k in range(ei, cap + 1):
            if np.isfinite(l[k]) and l[k] <= stop:
                return k, (o[k] if np.isfinite(o[k]) and o[k] < stop else stop), "stop"
            if np.isfinite(h[k]) and h[k] >= target:
                return k, (o[k] if np.isfinite(o[k]) and o[k] > target else target), "target"
        return cap, c[cap], ("signal" if cap == forced and forced_is_signal else "cap")

    # default: "signal" — exit on the opposing call, capped at max_hold
    xi = min(forced, hard)
    px = o[xi] if np.isfinite(o[xi]) and o[xi] > 0 else c[xi]
    reason = "signal" if (forced_is_signal and xi == forced) else ("cap" if xi == hard < forced else "eod")
    return xi, px, reason


def _simulate(calls, frames, spy_arr, idx, cfg, lag):
    n = len(idx)
    trades = []
    for tk, (o, h, l, c) in frames.items():
        atr = _atr14(h, l, c)
        events = _onsets(calls.get(tk, {}))
        last_exit = -1
        for i, (d, call, _conv) in enumerate(events):
            if call != "ROTATE IN":
                continue
            ei = int(idx.searchsorted(d + lag, side="right"))
            if ei >= n or ei <= last_exit:
                continue
            entry_px = o[ei]
            if not np.isfinite(entry_px) or entry_px <= 0:
                continue
            atr0 = atr[ei - 1] if ei - 1 >= 0 else np.nan

            forced, forced_is_signal = n - 1, False
            for d2, call2, _c2 in events[i + 1:]:
                if call2 in EXIT_CALLS:
                    fp = int(idx.searchsorted(d2 + lag, side="right"))
                    if fp < n:
                        forced, forced_is_signal = fp, True
                    break

            xi, xpx, reason = _exit(ei, entry_px, atr0, forced, forced_is_signal,
                                    cfg, o, h, l, c, n)
            if not np.isfinite(xpx) or xpx <= 0:
                continue
            ret = (xpx / entry_px - 1) * 100
            seg_h, seg_l = h[ei:xi + 1], l[ei:xi + 1]
            mfe = (np.nanmax(seg_h) / entry_px - 1) * 100 if seg_h.size and np.isfinite(np.nanmax(seg_h)) else None
            mae = (np.nanmin(seg_l) / entry_px - 1) * 100 if seg_l.size and np.isfinite(np.nanmin(seg_l)) else None

            trades.append({
                "symbol":     tk,
                "entry_ts":   idx[ei],
                "exit_ts":    idx[xi],
                "entry_date": idx[ei].strftime("%Y-%m-%d"),
                "exit_date":  idx[xi].strftime("%Y-%m-%d"),
                "entry_px":   _num(entry_px),
                "exit_px":    _num(xpx),
                "return_pct": _num(ret),
                "bars_held":  int(xi - ei + 1),
                "mfe_pct":    _num(mfe),
                "mae_pct":    _num(mae),
                "exit_reason": reason,
            })
            last_exit = xi
    return trades


def _trade_stats(trades):
    rets = np.array([t["return_pct"] for t in trades if t["return_pct"] is not None], dtype=float)
    if not rets.size:
        return {"n_trades": 0}
    wins, losses = rets[rets > 0], rets[rets <= 0]
    gw, gl = wins.sum(), -losses.sum()
    by_reason = defaultdict(int)
    for t in trades:
        by_reason[t["exit_reason"]] += 1
    return {
        "n_trades":      int(rets.size),
        "win_rate":      _num(100.0 * (rets > 0).mean()),
        "avg_return":    _num(rets.mean()),
        "median_return": _num(np.median(rets)),
        "avg_win":       _num(wins.mean()) if wins.size else None,
        "avg_loss":      _num(losses.mean()) if losses.size else None,
        "profit_factor": _num(gw / gl) if gl > 0 else None,
        "payoff_ratio":  _num(wins.mean() / -losses.mean()) if wins.size and losses.size else None,
        "avg_bars_held": _num(np.mean([t["bars_held"] for t in trades]), 1),
        "best":          _num(rets.max()),
        "worst":         _num(rets.min()),
        "by_exit":       dict(by_reason),
    }


def _symbol_contributions(trades):
    """Per-symbol trade aggregates → is the curve broad or carried by a few names?
    `total_return` is the gross (simple) sum of the symbol's trade returns — a
    first-order contribution read (the equity equal-weights co-held positions, so
    it's not an exact P&L split, but it answers 'where did the return come from')."""
    by_sym = defaultdict(list)
    for t in trades:
        if t.get("return_pct") is not None:
            by_sym[t["symbol"]].append(t["return_pct"])
    rows = []
    for sym, rets in by_sym.items():
        arr = np.array(rets, dtype=float)
        rows.append({"symbol": sym, "n_trades": int(arr.size),
                     "total_return": _num(arr.sum()), "avg_return": _num(arr.mean()),
                     "win_rate": _num(100.0 * (arr > 0).mean()),
                     "best": _num(arr.max()), "worst": _num(arr.min())})
    rows.sort(key=lambda r: r["total_return"] if r["total_return"] is not None else 0,
              reverse=True)
    total = sum(r["total_return"] for r in rows if r["total_return"] is not None)
    share = lambda k: (_num(100.0 * sum(r["total_return"] for r in rows[:k]) / total, 1)
                       if total else None)
    return {"rows": rows, "n_symbols": len(rows), "total_return_sum": _num(total),
            "top3_share": share(3), "top5_share": share(5)}


def _equity_curve(trades, close_df, spy_close, idx):
    if not trades:
        return None
    lo = min(t["entry_ts"] for t in trades)
    hi = max(t["exit_ts"] for t in trades)
    win_dates = [d for d in idx if lo <= d <= hi]
    syms = sorted({t["symbol"] for t in trades})
    if len(win_dates) < 2:
        return None

    sub  = close_df.reindex(index=win_dates, columns=syms)
    held = pd.DataFrame(False, index=win_dates, columns=syms)
    pos  = {d: i for i, d in enumerate(win_dates)}
    for t in trades:
        a, b = pos.get(t["entry_ts"]), pos.get(t["exit_ts"])
        if a is None or b is None or b <= a:
            continue
        held.iloc[a + 1:b + 1, held.columns.get_loc(t["symbol"])] = True

    sym_ret = sub.pct_change(fill_method=None)
    n_held  = held.sum(axis=1)
    port    = (sym_ret.where(held).sum(axis=1) / n_held.replace(0, np.nan)).fillna(0.0)
    equity  = (1 + port).cumprod()

    bench   = spy_close.reindex(win_dates).ffill()
    bench   = bench / bench.iloc[0]

    # Exposure-matched benchmark — SPY earned only on the days the strategy is
    # actually invested (flat otherwise). This isolates sector selection from the
    # cash drag of a selective long-only signal: strategy vs full-SPY mixes timing
    # + selection, strategy vs matched-SPY is the apples-to-apples read.
    invested  = (n_held > 0)
    spy_dret  = bench.pct_change().fillna(0.0)
    matched   = (1 + spy_dret * invested.astype(float)).cumprod()

    eq   = equity.to_numpy()
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq / peak - 1).min()) * 100
    ann    = 252.0 / len(win_dates)
    sharpe = (port.mean() / port.std() * math.sqrt(252)) if port.std() > 0 else None
    return {
        "dates":        [d.strftime("%Y-%m-%d") for d in win_dates],
        "strategy":     [_num(v) for v in eq],
        "benchmark":    [_num(v) for v in bench.to_numpy()],
        "benchmark_matched": [_num(v) for v in matched.to_numpy()],
        "total_return": _num((eq[-1] - 1) * 100),
        "cagr":         _num((eq[-1] ** ann - 1) * 100),
        "max_drawdown": _num(max_dd),
        "sharpe":       _num(sharpe, 2),
        "bench_total_return": _num((bench.iloc[-1] - 1) * 100),
        "bench_matched_return": _num((matched.iloc[-1] - 1) * 100),
        "time_in_market": _num(invested.mean() * 100, 1),
        "avg_positions":  _num(n_held.mean(), 2),
    }


# ---------------------------------------------------------------------------
# Data assembly (shared by run_backtest and the walk-forward search)
# ---------------------------------------------------------------------------

def _load(interval, params=None, tickers=DEFAULT_TICKERS):
    """→ (series, ohlc, idx, close_df, spy_close, spy_arr). One yfinance pull.
    close_df also carries RSP (equal-weight benchmark + rotation-regime input)."""
    series, _, _ = signal.compute_series(tickers, BENCHMARK, interval, params=params)
    ohlc  = signal.fetch_ohlc(list(tickers) + [BENCHMARK, "RSP"])
    close = ohlc["close"]
    idx   = close.index
    spy_close, spy_arr = _bench(close, idx, BENCHMARK)
    return series, ohlc, idx, close, spy_close, spy_arr


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_backtest(interval="1d", tail=6, exit_cfg=None, params=None,
                 universe=DEFAULT_UNIVERSE, benchmark=DEFAULT_BENCHMARK):
    cfg = _exit_cfg(exit_cfg)
    uni_key, tickers = _resolve_universe(universe)
    series, ohlc, idx, close, spy_close, spy_arr = _load(interval, params, tickers)
    if not series:
        return {"error": "no rotation series — yfinance returned nothing"}
    lag   = _entry_lag(interval)

    bench_sym = benchmark if benchmark in BENCHMARKS else DEFAULT_BENCHMARK
    bench_close, bench_arr = _bench(close, idx, bench_sym)
    if bench_close.empty:                         # RSP missing → fall back to SPY
        bench_sym, bench_close, bench_arr = BENCHMARK, spy_close, spy_arr
    rot_series = _rotation_series(close, idx)
    regime = rot_series.to_numpy() if rot_series is not None else None

    # rotation gate applies in the backtest too (no-lookahead): entries are
    # suppressed in a concentration regime, exactly as in the live engine.
    calls = signal.replay_calls(series, tail, params=params, rotation=rot_series)

    recs   = _event_records(calls, close, bench_arr, idx, lag, regime)
    events = _event_study(recs)
    confidence = _confidence_study(recs)
    regime_study = _regime_split_study(recs)

    frames = {tk: (ohlc["open"][tk].to_numpy(), ohlc["high"][tk].to_numpy(),
                   ohlc["low"][tk].to_numpy(), ohlc["close"][tk].to_numpy())
              for tk in tickers if tk in close.columns}
    trades = _simulate(calls, frames, bench_arr, idx, cfg, lag)

    stats  = _trade_stats(trades)
    equity = _equity_curve(trades, close, bench_close, idx)
    contributions = _symbol_contributions(trades)

    rets = [t["return_pct"] for t in trades if t["return_pct"] is not None]
    hist = None
    if rets:
        counts, edges = np.histogram(np.clip(rets, -25, 25), bins=20)
        hist = {"counts": [int(c) for c in counts], "edges": [_num(e, 2) for e in edges]}

    trade_view = sorted(trades, key=lambda t: t["entry_date"])[:TRADE_CAP]
    for t in trade_view:                          # drop non-JSON Timestamps
        t.pop("entry_ts", None); t.pop("exit_ts", None)

    return {
        "config": {
            "interval": interval, "tail": tail, "exit": cfg,
            "n_tickers": len(frames), "params": params or DEFAULTS,
            "universe": uni_key, "universe_label": UNIVERSES[uni_key]["label"],
            "benchmark": bench_sym, "benchmark_label": BENCHMARKS[bench_sym],
            "start": idx[0].strftime("%Y-%m-%d") if len(idx) else None,
            "end":   idx[-1].strftime("%Y-%m-%d") if len(idx) else None,
        },
        "event_study":    events,
        "confidence":     confidence,
        "regime_study":   regime_study,
        "contributions":  contributions,
        "stats":          stats,
        "equity":         equity,
        "histogram":      hist,
        "trades":         trade_view,
        "n_trades_total": len(trades),
        "caveats": [
            "Event study measures forward returns from the first bar after the "
            "signal is confirmed — no lookahead.",
            "Long-only; trades enter on a ROTATE IN onset at the next bar's open.",
            f"{len(frames)} tickers ({UNIVERSES[uni_key]['label']}) over ~3y — "
            + ("a small sample; read this as direction, not precision."
               if len(frames) <= 12 else
               "a broader set for less noisy conviction buckets, but still ~3y and "
               "ETF-only; read as direction, not precision."),
            f"Excess + equity are scored vs {BENCHMARKS[bench_sym]}; the rotation "
            "regime (on/off) is RSP/SPY vs its trend — a concentration regime is "
            "one a rotation signal structurally can't beat.",
        ],
    }


# Search grid — small on purpose (bounds runtime AND overfit). ZIGZAG_K shapes
# the significant-swing detection (compute_series-affecting, so series are cached
# by it); T_IN / T_WARN are the call-time conviction thresholds. The confluence
# weights and Fibonacci ratios are theory/judgment-fixed and never searched.
_GRID = {
    "ZIGZAG_K": [1.25, 1.50, 2.00],
    "T_IN":     [40.0, 50.0, 60.0],
    "T_WARN":   [30.0, 40.0],
}


def walk_forward_search(interval="1d", tail=6, exit_cfg=None, folds=4,
                        universe=DEFAULT_UNIVERSE, benchmark=DEFAULT_BENCHMARK):
    """Expanding-window walk-forward over the wave-engine params. Per fold: pick
    the in-sample-best combo, score it out-of-sample. Recommend the combo with
    the best mean OOS separation across folds (robust, not the global optimum)."""
    uni_key, tickers = _resolve_universe(universe)
    series0, ohlc, idx, close, spy_close, spy_arr = _load(interval, tickers=tickers)
    if len(idx) < 200:
        return {"error": "not enough history for a walk-forward search"}
    lag = _entry_lag(interval)
    bench_sym = benchmark if benchmark in BENCHMARKS else DEFAULT_BENCHMARK
    _bc, bench_arr = _bench(close, idx, bench_sym)
    if _bc.empty:
        bench_sym, bench_arr = BENCHMARK, spy_arr
    rot_series = _rotation_series(close, idx)

    keys  = list(_GRID)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(_GRID[k] for k in keys))]

    # records per combo (compute_series cached by ZIGZAG_K, the only param it
    # depends on; the conviction thresholds are call-time in replay_calls)
    series_cache = {}
    per_combo = []
    for combo in combos:
        sk = combo["ZIGZAG_K"]
        if sk not in series_cache:
            s, _, _ = signal.compute_series(tickers, BENCHMARK, interval,
                                            params={"ZIGZAG_K": sk})
            series_cache[sk] = s
        calls = signal.replay_calls(series_cache[sk], tail, params=combo,
                                    rotation=rot_series)
        recs  = _event_records(calls, close, bench_arr, idx, lag)
        per_combo.append((combo, recs))

    # time-segment boundaries over the daily index (folds+2 marks, clamped)
    bounds = [idx[min(int(len(idx) * k / (folds + 1)), len(idx) - 1)]
              for k in range(folds + 2)]
    NEG = pd.Timestamp.min

    fold_rows, oos_means = [], [0.0] * len(combos)
    oos_counts = [0] * len(combos)
    for k in range(folds):
        is_hi, oos_hi = bounds[k + 1], bounds[k + 2]
        is_best, is_score = None, float("-inf")
        for combo, recs in per_combo:
            s = _separation(recs, NEG, is_hi)
            if not math.isnan(s) and s > is_score:
                is_score, is_best = s, (combo, recs)
        if is_best is None:
            continue
        oos = _separation(is_best[1], is_hi, oos_hi)
        fold_rows.append({
            "fold": k + 1,
            "is_end":  is_hi.strftime("%Y-%m-%d"),
            "oos_end": oos_hi.strftime("%Y-%m-%d"),
            "params":  is_best[0],
            "is_sep":  _num(is_score),
            "oos_sep": _num(oos),
        })
        for j, (combo, recs) in enumerate(per_combo):
            so = _separation(recs, is_hi, oos_hi)
            if not math.isnan(so):
                oos_means[j] += so
                oos_counts[j] += 1

    scored = [(per_combo[j][0], oos_means[j] / oos_counts[j])
              for j in range(len(combos)) if oos_counts[j] > 0]
    if not scored:
        return {"error": "search produced no out-of-sample signal — too few onsets"}
    recommended, best_oos = max(scored, key=lambda x: x[1])

    report = run_backtest(interval=interval, tail=tail, exit_cfg=exit_cfg,
                          params=recommended, universe=uni_key, benchmark=bench_sym)
    report["walk_forward"] = {
        "folds":          fold_rows,
        "recommended":    recommended,
        "defaults":       {k: DEFAULTS[k] for k in keys},
        "mean_oos_sep":   _num(best_oos),
        "horizon":        SEP_HORIZON,
        "grid_size":      len(combos),
        "note": ("Recommended = best mean out-of-sample IN−OUT excess separation "
                 f"at +{SEP_HORIZON}d across {folds} expanding folds. Bake these in "
                 "as the new DEFAULTS only if OOS holds up across folds."),
    }
    return report
