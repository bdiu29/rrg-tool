"""
Harness referee (Notes.txt step 5) — validate the COMBINED decision.

The combiner produces a confluence call; this asks whether that call, scored over
history, actually beats (a) raw RRG alone and (b) beta (SPY/RSP). The whole point of
the project's founding finding: a plausible signal isn't an edge until the referee
says so.

How it works (mostly REUSE, not new quant): `signal.replay_calls` already emits a
per-date per-ticker `{call, conviction}` panel, and `rrg.backtest` already turns such
a panel into an event study + a cross-sectional long/hedged portfolio sim vs
SPY/RSP-matched. So we build the harness's OWN confluence panel in the same shape
(`build_harness_calls`, via `combiner.score_sectors`) and run BOTH panels through the
exact same machinery — an honest A/B.

POINT-IN-TIME SCOPE (the honest caveat): only the price-derived votes can be replayed
historically — breadth regime, RRG calls/conviction, rankings (cross-sectional RS),
and the rotation gate. Flow/news/screener/canslim stay LIVE-ONLY refinements (exactly
like RRG's flag/exhaustion factors), excluded here. The LLM narration plays no part —
only the deterministic combiner is scored, which is the entire point.
"""

import math

import numpy as np
import pandas as pd

from modules.rrg import backtest as bt
from modules.rrg import signal
from modules.harness import combiner

# Confluence-score → call thresholds. A genuine RRG ROTATE IN (conv ~60) plus an
# above-median rank clears T_LONG; the regime stance halving the RRG bet in a
# CONCENTRATE regime naturally drops marginal entries below it (the validated tilt).
T_LONG = 40.0
T_OUT  = 30.0

# Rankings RS composite — trailing excess-return blend, ranked cross-sectionally
# per date (point-in-time, no-lookahead). Mirrors rankings' RS_LOOKBACKS idea.
RS_LOOKBACKS = (21, 63, 126)


# ---------------------------------------------------------------------------
# Point-in-time vote panels (the replayable subset)
# ---------------------------------------------------------------------------

def _rank_panel(close, tickers, benchmark):
    """Per-date cross-sectional RS rank (0-100) of each sector vs the benchmark over a
    trailing lookback blend. No-lookahead: each row uses only trailing returns; the
    rank is cross-sectional among the sectors that day, so 50 ≈ median (matches the
    `combiner.score_sectors` rank-tilt convention)."""
    cols = [t for t in tickers if t in close.columns]
    if benchmark not in close.columns or not cols:
        return None
    bench = close[benchmark]
    comp = None
    for lb in RS_LOOKBACKS:
        ex = close[cols].pct_change(lb, fill_method=None).sub(
            bench.pct_change(lb, fill_method=None), axis=0)
        comp = ex if comp is None else comp + ex
    return comp.rank(axis=1, pct=True) * 100.0          # [date × ticker], 0-100


def _breadth_regime_panel(dates):
    """Per-date regime (HEALTHY/NEUTRAL/DETERIORATING) aligned to `dates` — mirrors
    rrg/flag_backtest's helper. Lazy + fail-soft → None (then stance is NEUTRAL)."""
    try:
        from modules.breadth import _full_series
        from modules.breadth import regime as breadth_regime
        agg, der, _index = _full_series("sp500")
        if agg is None:
            return None
        labels = breadth_regime.regime_series(der["summation"], agg["pct_above_200"])
        return breadth_regime.align_labels(labels, dates)
    except Exception:
        return None


def _asof(series, d):
    """Last known value of a Series at/▸before date d → str, or None (no-lookahead)."""
    if series is None or not len(series):
        return None
    try:
        v = series.asof(d)
    except Exception:
        return None
    return v if isinstance(v, str) else None


def _score_to_call(score):
    """Confluence score → an RRG-compatible call (the 7-value set's actionable subset).
    ROTATE IN / ROTATE OUT drive the long/exit machinery; HOLD/WATCH are neutral."""
    if score >= T_LONG:
        return "ROTATE IN"
    if score <= -T_OUT:
        return "ROTATE OUT"
    return "HOLD" if score > 0 else "WATCH"


def build_harness_calls(rrg_calls, rank_panel, regime_panel, rotation_series):
    """Per-date per-ticker CONFLUENCE call panel — the same `{ticker: {date: {call,
    conviction}}}` shape as `signal.replay_calls`, so it is a drop-in for the rrg
    backtest machinery. For each date: derive the regime STANCE, score every sector via
    `combiner.score_sectors` (RRG conviction × stance suppression + rank tilt +
    agreement bonus), and map the score to a call. Deterministic, no-lookahead (every
    input is as-of the date)."""
    dates = sorted({d for tl in rrg_calls.values() for d in tl})
    out = {tk: {} for tk in rrg_calls}
    have_rank = rank_panel is not None

    for d in dates:
        stance = combiner.decide_stance(_asof(regime_panel, d), _asof(rotation_series, d))

        rrg_rows, rank_by = [], {}
        rank_row = rank_panel.loc[d] if (have_rank and d in rank_panel.index) else None
        for tk, tl in rrg_calls.items():
            ev = tl.get(d)
            if ev is None:
                continue
            rrg_rows.append({"ticker": tk, "name": tk, "call": ev["call"],
                             "conviction": ev.get("conviction") or 0.0})
            if rank_row is not None and tk in rank_row.index and pd.notna(rank_row[tk]):
                rank_by[tk] = float(rank_row[tk])

        for s in combiner.score_sectors(rrg_rows, rank_by, stance=stance):
            out[s["ticker"]][d] = {"call": _score_to_call(s["score"]),
                                   "conviction": round(min(99.0, abs(s["score"])), 1)}
    return out


# ---------------------------------------------------------------------------
# A/B report
# ---------------------------------------------------------------------------

def _panel_report(calls, close, ohlc, idx, bench_close, bench_arr, regime_arr,
                  rot_series, frames, cfg, pcfg, lag):
    """Score one call panel through the rrg machinery: event study + regime split +
    long-only trade equity + the long-hedged rotation portfolio."""
    recs   = bt._event_records(calls, close, bench_arr, idx, lag, regime_arr)
    trades = bt._simulate(calls, frames, bench_arr, idx, cfg, lag)
    return {
        "event_study":        bt._event_study(recs),
        "regime_study":       bt._regime_split_study(recs),
        "stats":              bt._trade_stats(trades),
        "equity":             bt._equity_curve(trades, close, bench_close, idx),
        "rotation_portfolio": bt._rotation_portfolio(calls, close, bench_close, idx,
                                                     pcfg, rot_series),
    }


def _excess10(event_study, call):
    h = (event_study.get(call, {}).get("horizons", {}) or {}).get("10", {})
    return h.get("excess"), h.get("n")


def _equity_row(eq):
    if not eq:
        return None
    return {"total_return": eq["total_return"], "max_drawdown": eq["max_drawdown"],
            "sharpe": eq["sharpe"], "time_in_market": eq["time_in_market"]}


def _hedged_row(rp):
    if not rp:
        return None
    return {"hedged_return": rp["hedged_return"], "long_return": rp["long_return"],
            "max_drawdown": rp["max_drawdown"], "sharpe": rp["sharpe"],
            "time_in_market": rp["time_in_market"]}


def _ab_summary(harness, rrg):
    """Headline A/B: the harness confluence vs raw RRG vs beta (matched)."""
    h_eq, r_eq = harness.get("equity"), rrg.get("equity")
    h_in, h_in_n = _excess10(harness.get("event_study", {}), "ROTATE IN")
    r_in, r_in_n = _excess10(rrg.get("event_study", {}), "ROTATE IN")
    h_out, _ = _excess10(harness.get("event_study", {}), "ROTATE OUT")
    r_out, _ = _excess10(rrg.get("event_study", {}), "ROTATE OUT")

    bench_matched = (h_eq or r_eq or {}).get("bench_matched_return")
    bench_full    = (h_eq or r_eq or {}).get("bench_total_return")
    h_hedged = _hedged_row(harness.get("rotation_portfolio"))
    r_hedged = _hedged_row(rrg.get("rotation_portfolio"))

    verdict = _verdict(h_eq, r_eq, bench_matched, h_hedged, r_hedged, h_in, r_in)
    return {
        "long_only": {"harness": _equity_row(h_eq), "rrg": _equity_row(r_eq),
                      "benchmark_matched": bench_matched, "benchmark_full": bench_full},
        "hedged":    {"harness": h_hedged, "rrg": r_hedged},
        "event_in_excess_10d":  {"harness": h_in,  "harness_n": h_in_n,
                                 "rrg": r_in, "rrg_n": r_in_n},
        "event_out_excess_10d": {"harness": h_out, "rrg": r_out},
        "verdict": verdict,
    }


def _verdict(h_eq, r_eq, bench_matched, h_hedged, r_hedged, h_in, r_in):
    """An honest read — separating ABSOLUTE return (mostly beta in a concentration
    regime) from the SELECTION-ISOLATING relative edge (the long-hedged book + the
    ROTATE-IN forward excess), where the confluence filter's value actually shows.
    A null result is a valid (and likely) outcome — the correct gate on Phase 3."""
    if not h_eq:
        return "No harness trades to score on this window."
    hr = h_eq["total_return"]
    rr = r_eq["total_return"] if r_eq else None
    beats_rrg_ret = rr is not None and hr is not None and hr > rr
    beats_beta    = bench_matched is not None and hr is not None and hr > bench_matched

    # relative-edge (selection-isolating): does the harness sharpen the signal vs RRG?
    hh = h_hedged["hedged_return"] if h_hedged else None
    rh = r_hedged["hedged_return"] if r_hedged else None
    better_hedged = hh is not None and rh is not None and hh > rh
    better_excess = h_in is not None and r_in is not None and h_in > r_in
    sharper = better_hedged or better_excess
    rel = ""
    if h_hedged and r_hedged:
        rel = (f" Relative edge (long−bench): harness {hh:+.1f}% vs RRG {rh:+.1f}%"
               + (f"; ROTATE-IN excess@10d {h_in:+.2f}% vs {r_in:+.2f}%."
                  if (h_in is not None and r_in is not None) else "."))

    if beats_beta and beats_rrg_ret:
        return ("Harness confluence beat BOTH raw RRG and beta (matched) on return — "
                "regime-arbitrated confluence added value here. Still ETF-only / ~3y: "
                "direction, not proof." + rel)
    if not beats_beta and sharper:
        return ("Harness did NOT beat beta on absolute return (the concentration regime "
                "is mostly beta), BUT regime-arbitrated confluence SHARPENED the relative "
                "signal vs raw RRG." + rel + " That's signal-QUALITY, not absolute alpha — "
                "the Phase-3 gate is whether that relative edge survives costs + the "
                "live-only votes, not headline return.")
    if beats_beta:
        return ("Harness beat beta (matched) but not raw RRG on return." + rel)
    return ("Harness did NOT beat beta (matched) and did not sharpen the relative signal "
            "vs RRG on this window — consistent with the founding finding that selection "
            "struggles in a concentration regime. A null result is the correct gate on "
            "Phase 3, not a bug." + rel)


def run_harness_backtest(interval="1d", tail=6, universe=None, benchmark=None,
                         portfolio=None, params=None):
    """A/B the harness confluence call vs raw RRG vs beta. A focused fork of
    `rrg.backtest.run_backtest` that builds two call panels and scores both."""
    cfg  = bt._exit_cfg(None)               # default: binary exit on ROTATE OUT/AVOID
    pcfg = bt._portfolio_cfg(portfolio or {"enabled": True, "mode": "long_hedged",
                                           "n_long": 3, "n_short": 3, "gate": True})
    uni_key, tickers = bt._resolve_universe(universe or bt.DEFAULT_UNIVERSE)
    series, ohlc, idx, close, spy_close, spy_arr = bt._load(interval, params, tickers)
    if not series:
        return {"error": "no rotation series — yfinance returned nothing"}
    lag = bt._entry_lag(interval)

    bench_sym = benchmark if benchmark in bt.BENCHMARKS else bt.DEFAULT_BENCHMARK
    bench_close, bench_arr = bt._bench(close, idx, bench_sym)
    if bench_close.empty:                   # RSP missing → fall back to SPY
        bench_sym, bench_close, bench_arr = signal.BENCHMARK, spy_close, spy_arr
    rot_series = bt._rotation_series(close, idx)
    regime_arr = rot_series.to_numpy() if rot_series is not None else None

    # the two panels — same dates, same universe, only the CALL logic differs
    rrg_calls    = signal.replay_calls(series, tail, params=params, rotation=rot_series)
    rank_panel   = _rank_panel(close, tickers, signal.BENCHMARK)
    regime_panel = _breadth_regime_panel(idx)
    harness_calls = build_harness_calls(rrg_calls, rank_panel, regime_panel, rot_series)

    frames = {tk: (ohlc["open"][tk].to_numpy(), ohlc["high"][tk].to_numpy(),
                   ohlc["low"][tk].to_numpy(), ohlc["close"][tk].to_numpy())
              for tk in tickers if tk in close.columns}

    args = (close, ohlc, idx, bench_close, bench_arr, regime_arr, rot_series, frames,
            cfg, pcfg, lag)
    harness_rep = _panel_report(harness_calls, *args)
    rrg_rep     = _panel_report(rrg_calls, *args)

    return {
        "config": {
            "interval": interval, "tail": tail, "n_tickers": len(frames),
            "universe": uni_key, "universe_label": bt.UNIVERSES[uni_key]["label"],
            "benchmark": bench_sym, "benchmark_label": bt.BENCHMARKS[bench_sym],
            "portfolio": pcfg,
            "regime_panel": regime_panel is not None, "rank_panel": rank_panel is not None,
            "start": idx[0].strftime("%Y-%m-%d") if len(idx) else None,
            "end":   idx[-1].strftime("%Y-%m-%d") if len(idx) else None,
        },
        "ab":      _ab_summary(harness_rep, rrg_rep),
        "harness": harness_rep,
        "rrg":     rrg_rep,
        "caveats": [
            "POINT-IN-TIME SUBSET: only price-derived votes are replayed — breadth "
            "regime, RRG calls/conviction, rankings (cross-sectional RS), rotation gate. "
            "Flow/news/screener/canslim are live-only refinements, excluded here.",
            "The LLM narration is NOT scored — only the deterministic combiner is (the point).",
            "Same machinery + caveats as the RRG backtester: no-lookahead event study, "
            f"long-only enters at the next bar's open, ~3y ETF-only ({bt.UNIVERSES[uni_key]['label']}) "
            "— read as direction, not precision.",
            f"Excess + equity scored vs {bt.BENCHMARKS[bench_sym]}-matched (exposure-matched, "
            "isolating selection from cash drag); the long_hedged book is long − benchmark.",
            "Combiner weights are judgment-fixed; this MEASURES them (it does not fit them).",
        ],
    }


# ---------------------------------------------------------------------------
# Text report (CLI)
# ---------------------------------------------------------------------------

def format_report(rep):
    if "error" in rep:
        return "  harness backtest error: " + rep["error"]
    c, ab = rep["config"], rep["ab"]
    L = []
    L.append("")
    L.append(f"  HARNESS REFEREE — {c['universe_label']} · {c['interval']} · "
             f"{c['start']}→{c['end']} · vs {c['benchmark_label']}")
    L.append(f"  point-in-time votes: RRG + rotation gate"
             + (" + breadth regime" if c["regime_panel"] else " (breadth regime: n/a)")
             + (" + RS rank" if c["rank_panel"] else ""))
    L.append("  " + "=" * 70)

    def row(label, eq):
        if not eq:
            return f"  {label:<22} (no trades)"
        return (f"  {label:<22} ret {eq['total_return']:>7}%   maxDD {eq['max_drawdown']:>7}%   "
                f"Sharpe {str(eq['sharpe']):>5}   in-mkt {eq['time_in_market']}%")

    L.append("  LONG-ONLY TRADE SIM (enter ROTATE IN onset, exit ROTATE OUT):")
    L.append(row("Harness confluence", ab["long_only"]["harness"]))
    L.append(row("Raw RRG", ab["long_only"]["rrg"]))
    bm, bf = ab["long_only"]["benchmark_matched"], ab["long_only"]["benchmark_full"]
    L.append(f"  {'Benchmark (matched)':<22} ret {bm if bm is not None else '—':>7}%"
             f"   (full {bf if bf is not None else '—'}%)")
    L.append("")

    def hrow(label, hr):
        if not hr:
            return f"  {label:<22} (no book)"
        return (f"  {label:<22} long−bench {hr['hedged_return']:>7}%   "
                f"maxDD {hr['max_drawdown']:>7}%   Sharpe {str(hr['sharpe']):>5}")

    L.append("  LONG-HEDGED ROTATION PORTFOLIO (long − benchmark, the relative edge):")
    L.append(hrow("Harness confluence", ab["hedged"]["harness"]))
    L.append(hrow("Raw RRG", ab["hedged"]["rrg"]))
    L.append("")

    ie = ab["event_in_excess_10d"]; oe = ab["event_out_excess_10d"]
    L.append("  EVENT STUDY — forward excess vs benchmark @ +10d:")
    L.append(f"  {'ROTATE IN':<22} harness {str(ie['harness']):>7} (n{ie['harness_n']})   "
             f"RRG {str(ie['rrg']):>7} (n{ie['rrg_n']})")
    L.append(f"  {'ROTATE OUT':<22} harness {str(oe['harness']):>7}        "
             f"RRG {str(oe['rrg']):>7}")
    L.append("  " + "=" * 70)
    L.append("  VERDICT: " + ab["verdict"])
    L.append("")
    for cav in rep["caveats"]:
        L.append("  · " + cav)
    L.append("")
    return "\n".join(L)
