"""
Strategy backtester for the screener.

Validates a confluence of screener signals over history. It reuses the exact
indicator math the live scanner uses (`metrics.compute_indicator_panels`) and
the exact filter engine (`filters.apply_filters`): on each trading date it
rebuilds the same cross-section the scanner would filter, takes the matches as
entry signals, simulates a long trade per the chosen exit model, and aggregates
trade + forward-return + equity-curve statistics.

Every trade also records its full signal feature vector at entry, so a
classical-ML ranking layer (logistic / gradient boosting on those features) can
train on the same records later without re-running history.

No lookahead: a signal on date d is entered at the next bar's OPEN; all
indicator panels are trailing and the golden-pocket pivots are confirmation-
lagged. Entries fire only on signal *onset* (matched today, not yesterday) so a
persistent signal opens one trade, not one per day.

Caveats (surfaced in the report): long-only; fundamentals / earnings / RRG call
use latest-known values, not point-in-time; universe membership is today's list
(survivorship). Bars come from the breadth store; nothing is re-downloaded.
"""

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from modules.breadth import store as breadth_store
from modules.breadth import universes as breadth_universes
from modules.screener import metrics, store
from modules.screener import filters as filt

DEFAULT_UNIVERSE   = "sp500"
DEFAULT_YEARS      = 3
MAX_SYMBOLS        = 6000
FWD_HORIZONS       = (1, 5, 10, 20)
TRADE_LIST_CAP     = 400
# Fields that have no honest point-in-time history in a backtest.
NON_HISTORICAL_FIELDS = {"rrg_call", "days_to_earnings"}

DEFAULT_EXIT = {
    "model":      "hold",   # hold | atr | signal
    "hold_days":  10,
    "atr_stop":   2.0,
    "atr_target": 3.0,
    "max_hold":   40,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _num(x, places=4):
    """JSON-safe scalar: NaN/inf → None, floats rounded."""
    if x is None:
        return None
    if isinstance(x, str):
        return x
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, places)


def _resolve_symbols(universe):
    cfg = breadth_universes.load_config()
    keys = set(cfg["universes"])
    if universe == "all":
        syms = set()
        for k in keys:
            syms.update(breadth_store.get_members(k))
    elif universe in keys:
        syms = set(breadth_store.get_members(universe))
    else:
        raise ValueError(f"unknown universe '{universe}'")
    return sorted(syms)[:MAX_SYMBOLS]


def _exit_cfg(raw):
    cfg = dict(DEFAULT_EXIT)
    if raw:
        cfg.update({k: raw[k] for k in raw if k in DEFAULT_EXIT})
    cfg["model"] = cfg["model"] if cfg["model"] in ("hold", "atr", "signal") else "hold"
    cfg["hold_days"] = max(1, int(cfg["hold_days"]))
    cfg["max_hold"]  = max(1, int(cfg["max_hold"]))
    cfg["atr_stop"]   = float(cfg["atr_stop"])
    cfg["atr_target"] = float(cfg["atr_target"])
    return cfg


# ---------------------------------------------------------------------------
# Per-date match sets (the live scan path, walked over history)
# ---------------------------------------------------------------------------

def _matches_by_date(panels, fund, conditions, dates):
    """{date: set(matched symbols)} — build the same cross-section the scanner
    filters, for each date, and apply the same condition engine."""
    out = {}
    for d in dates:
        cross = pd.DataFrame({k: panels[k].loc[d] for k in panels})
        cross = cross.join(fund, how="left")
        cross = filt.derive_scan_columns(cross, today=d)
        matched = filt.apply_filters(cross, conditions)
        out[d] = set(matched.index)
    return out


# ---------------------------------------------------------------------------
# Trade simulation (numpy for the bar walk)
# ---------------------------------------------------------------------------

def _simulate(ei, j, entry_px, atr0, model, cfg, O, H, L, C, dates,
              matches, sym, n):
    """Return (exit_idx, exit_px, reason) for a long position entered at the
    open of bar `ei` in symbol column `j`."""
    cap = min(ei + cfg["max_hold"], n - 1)

    if model == "hold":
        k = min(ei + cfg["hold_days"] - 1, n - 1)
        while k > ei and not np.isfinite(C[k, j]):
            k -= 1
        reason = "hold" if k == ei + cfg["hold_days"] - 1 else "eod"
        return k, C[k, j], reason

    if model == "atr" and np.isfinite(atr0) and atr0 > 0:
        stop   = entry_px - cfg["atr_stop"] * atr0
        target = entry_px + cfg["atr_target"] * atr0
        for k in range(ei, cap + 1):
            lo, hi, op = L[k, j], H[k, j], O[k, j]
            if np.isfinite(lo) and lo <= stop:                 # stop checked first
                return k, (op if np.isfinite(op) and op < stop else stop), "stop"
            if np.isfinite(hi) and hi >= target:
                return k, (op if np.isfinite(op) and op > target else target), "target"
        return cap, C[cap, j], "cap"

    if model == "signal":
        for k in range(ei, cap + 1):
            if sym not in matches.get(dates[k], ()):           # signal gone
                return k, C[k, j], "signal"
        return cap, C[cap, j], "cap"

    # atr model with no valid ATR → fall back to a time stop
    return min(ei + cfg["hold_days"] - 1, n - 1), C[min(ei + cfg["hold_days"] - 1, n - 1), j], "eod"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _trade_stats(rets, bars, reasons):
    if not rets:
        return {"n_trades": 0}
    r = np.array(rets, dtype=float)
    wins, losses = r[r > 0], r[r <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    by_reason = {}
    for reason in reasons:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "n_trades":     int(r.size),
        "win_rate":     _num(100.0 * (r > 0).mean()),
        "avg_return":   _num(r.mean()),
        "median_return": _num(np.median(r)),
        "avg_win":      _num(wins.mean()) if wins.size else None,
        "avg_loss":     _num(losses.mean()) if losses.size else None,
        "profit_factor": _num(gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy":   _num(r.mean()),
        "payoff_ratio": _num((wins.mean() / -losses.mean())) if wins.size and losses.size else None,
        "avg_bars_held": _num(np.mean(bars), 1),
        "best":         _num(r.max()),
        "worst":        _num(r.min()),
        "by_exit":      by_reason,
    }


def _forward_study(trades):
    out = {}
    for h in FWD_HORIZONS:
        fr = np.array([t["fwd"][h] for t in trades if t["fwd"].get(h) is not None], dtype=float)
        ex = np.array([t["fwd_excess"][h] for t in trades if t["fwd_excess"].get(h) is not None], dtype=float)
        if fr.size:
            out[str(h)] = {
                "mean":      _num(fr.mean()),
                "median":    _num(np.median(fr)),
                "win_rate":  _num(100.0 * (fr > 0).mean()),
                "excess_vs_spy": _num(ex.mean()) if ex.size else None,
                "n":         int(fr.size),
            }
    return out


def _equity_curve(trades, close, spy_close, win_dates):
    """Equal-weight daily mark-to-market: a symbol is held on every date in
    (entry_date, exit_date]; the portfolio's daily return is the mean of held
    symbols' close-to-close returns (cash → 0 on days with no positions)."""
    syms = sorted({t["symbol"] for t in trades})
    if not syms or len(win_dates) < 2:
        return None
    sub = close.reindex(index=win_dates, columns=syms)
    held = pd.DataFrame(False, index=win_dates, columns=syms)
    pos = {d: i for i, d in enumerate(win_dates)}
    for t in trades:
        a, b = pos.get(t["entry_date"]), pos.get(t["exit_date"])
        if a is None or b is None or b <= a:
            continue
        held.iloc[a + 1:b + 1, held.columns.get_loc(t["symbol"])] = True
    sym_ret = sub.pct_change(fill_method=None)
    n_held  = held.sum(axis=1)
    port    = (sym_ret.where(held).sum(axis=1) / n_held.replace(0, np.nan)).fillna(0.0)

    equity = (1 + port).cumprod()
    bench  = spy_close.reindex(win_dates).ffill()
    bench  = bench / bench.iloc[0]

    eq = equity.to_numpy()
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq / peak - 1).min()) * 100
    ann = 252.0 / len(win_dates)
    sharpe = (port.mean() / port.std() * math.sqrt(252)) if port.std() > 0 else None
    return {
        "dates":      list(win_dates),
        "strategy":   [_num(v) for v in eq],
        "benchmark":  [_num(v) for v in bench.to_numpy()],
        "total_return": _num((eq[-1] - 1) * 100),
        "cagr":       _num((eq[-1] ** ann - 1) * 100),
        "max_drawdown": _num(max_dd),
        "sharpe":     _num(sharpe, 2),
        "bench_total_return": _num((bench.iloc[-1] - 1) * 100),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_backtest(conditions, universe=DEFAULT_UNIVERSE, start=None, end=None,
                 exit_cfg=None):
    """→ report dict. Long-only; entries fire on signal onset, executed at the
    next bar's open; exits per `exit_cfg` (hold | atr | signal)."""
    cfg     = _exit_cfg(exit_cfg)
    symbols = _resolve_symbols(universe)
    if not symbols:
        return {"error": f"no synced members for universe '{universe}'"}

    end = end or datetime.now().strftime("%Y-%m-%d")
    if start is None:
        start = (datetime.strptime(end, "%Y-%m-%d")
                 - timedelta(days=365 * DEFAULT_YEARS)).strftime("%Y-%m-%d")
    warmup = (datetime.strptime(start, "%Y-%m-%d")
              - timedelta(days=metrics.SNAPSHOT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    close, volume, open_, high, low = breadth_store.get_panels(
        symbols, start=warmup, fields=("close", "volume", "open", "high", "low"))
    if close.empty:
        return {"error": "no bars in range — run a breadth sync first"}

    # align every panel to the close grid
    idx, cols = close.index, close.columns
    open_ = open_.reindex(index=idx, columns=cols)
    high  = high.reindex(index=idx, columns=cols)
    low   = low.reindex(index=idx, columns=cols)
    spy   = breadth_store.get_series("SPY", start=warmup)
    spy_close = spy["close"] if not spy.empty else pd.Series(dtype=float)

    panels = metrics.compute_indicator_panels(close, volume, open_, high, low, spy_close)
    fund   = store.get_fundamentals()

    dates    = list(idx)
    eval_dates = [d for d in dates if d >= start]              # incl. past `end` for signal exits
    rebal    = [d for d in eval_dates if d <= end]
    if len(rebal) < 2:
        return {"error": "not enough trading days in range"}

    matches = _matches_by_date(panels, fund, conditions, eval_dates)

    # numpy views for the bar walk
    O, H, L, C = open_.to_numpy(), high.to_numpy(), low.to_numpy(), close.to_numpy()
    atr_arr = panels["atr14"].to_numpy()
    n = len(dates)
    date_pos = {d: i for i, d in enumerate(dates)}
    sym_pos  = {s: j for j, s in enumerate(cols)}
    spy_arr  = (spy_close.reindex(idx).ffill().to_numpy()
                if not spy_close.empty else np.full(n, np.nan))

    trades, prev = [], set()
    for d in rebal:
        onset = matches[d] - prev
        prev  = matches[d]
        i = date_pos[d]
        if i + 1 >= n:
            continue
        ei = i + 1
        for sym in onset:
            j = sym_pos.get(sym)
            if j is None:
                continue
            entry_px = O[ei, j]
            if not np.isfinite(entry_px) or entry_px <= 0:
                continue
            atr0 = atr_arr[i, j]
            xi, xpx, reason = _simulate(ei, j, entry_px, atr0, cfg["model"], cfg,
                                        O, H, L, C, dates, matches, sym, n)
            if not np.isfinite(xpx) or xpx <= 0:
                continue
            ret = (xpx / entry_px - 1) * 100
            seg_h, seg_l = H[ei:xi + 1, j], L[ei:xi + 1, j]
            mfe = (np.nanmax(seg_h) / entry_px - 1) * 100 if seg_h.size else None
            mae = (np.nanmin(seg_l) / entry_px - 1) * 100 if seg_l.size else None

            fwd, fwd_ex = {}, {}
            c0, s0 = C[ei, j], spy_arr[ei]
            for h in FWD_HORIZONS:
                if ei + h < n and np.isfinite(c0) and c0 > 0 and np.isfinite(C[ei + h, j]):
                    fr = (C[ei + h, j] / c0 - 1) * 100
                    fwd[h] = fr
                    if np.isfinite(s0) and s0 > 0 and np.isfinite(spy_arr[ei + h]):
                        fwd_ex[h] = fr - (spy_arr[ei + h] / s0 - 1) * 100

            feats = {k: _num(panels[k].iat[i, j]) for k in panels}
            f_row = fund.loc[sym].to_dict() if sym in fund.index else {}
            feats.update({k: _num(v) for k, v in f_row.items()})

            trades.append({
                "symbol":     sym,
                "signal_date": d,
                "entry_date": dates[ei],
                "entry_px":   _num(entry_px),
                "exit_date":  dates[xi],
                "exit_px":    _num(xpx),
                "return_pct": _num(ret),
                "bars_held":  int(xi - ei + 1),
                "mfe_pct":    _num(mfe),
                "mae_pct":    _num(mae),
                "exit_reason": reason,
                "fwd":        fwd,
                "fwd_excess": fwd_ex,
                "features":   feats,
            })

    rets    = [t["return_pct"] for t in trades if t["return_pct"] is not None]
    bars    = [t["bars_held"] for t in trades]
    reasons = [t["exit_reason"] for t in trades]
    stats   = _trade_stats(rets, bars, reasons)
    forward = _forward_study(trades)
    equity  = _equity_curve(trades, close, spy_close, rebal)

    # return-distribution histogram
    hist = None
    if rets:
        counts, edges = np.histogram(np.clip(rets, -50, 50), bins=20)
        hist = {"counts": [int(c) for c in counts],
                "edges":  [_num(e, 2) for e in edges]}

    caveats = [
        "Long-only; entries fire on signal onset and execute at the next bar's open.",
        "Universe membership is today's list — delisted names are absent (survivorship).",
    ]
    used = {c.get("field") for c in conditions} & NON_HISTORICAL_FIELDS
    if used:
        caveats.append(
            f"Conditions on {', '.join(sorted(used))} use latest-known (not "
            "point-in-time) values; 'rrg_call' is unavailable historically and "
            "matches nothing in a backtest.")

    trade_view = sorted(trades, key=lambda t: t["entry_date"])[:TRADE_LIST_CAP]
    return {
        "config": {
            "universe": universe, "start": rebal[0], "end": rebal[-1],
            "exit": cfg, "n_symbols": len(symbols), "conditions": conditions,
        },
        "stats":        stats,
        "forward":      forward,
        "equity":       equity,
        "histogram":    hist,
        "trades":       trade_view,
        "n_trades_total": len(trades),
        "caveats":      caveats,
    }
