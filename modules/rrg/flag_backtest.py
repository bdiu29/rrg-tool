"""
Bull/bear flag success-rate study — standalone, PRICE + VOLUME.

The live conviction engine carries a flag *shape* factor on the RS line (which has
no volume). This script is the rigorous companion the user asked for: it detects
classic price flags — an impulsive **flagpole** (strong move) followed by a brief,
shallow consolidation on **tapering volume** (the flag) — on each SPDR sector ETF
+ SPY, then measures whether the continuation actually happens. It answers "how
often does a flag work, and by how much?" across the basket.

The detection core (`_detect`/`_summary`/constants) now lives in the pure
`flags.py` leaf so `signal.py` (conviction weighting) and the screener can reuse
it without a circular import; this module re-exports them and adds the yfinance
download, the CLI, and the **regime-conditioned** study (`--regime`) that measures
the honest bear-flag edge during DETERIORATING regimes only.

Detection is no-lookahead at each flag (uses only bars up to the flag); the
forward returns it scores are, of course, in the future — that's the study.

Run:  /usr/bin/python3 -m modules.rrg.flag_backtest [--no-taper] [--period 5y] [--regime]
"""

import argparse
from collections import defaultdict

import numpy as np
import yfinance as yf

from .signal import DEFAULT_TICKERS, BENCHMARK
from .flags import _detect, _summary, _regime_ok, SUCCESS_H   # the shared detection core


def _regime_labels_for(dates):
    """Best-effort per-date regime labels (HEALTHY/NEUTRAL/DETERIORATING) aligned
    to `dates`, from the breadth module. Lazy + fail-soft: returns None if breadth
    has no local data (then the study runs unconditioned)."""
    try:
        from modules.breadth import _full_series
        from modules.breadth import regime as breadth_regime
        agg, der, _index = _full_series("sp500")
        if agg is None:
            return None
        labels = breadth_regime.regime_series(der["summation"], agg["pct_above_200"])
        return breadth_regime.align_labels(labels, dates)   # handles str/Timestamp mismatch
    except Exception:
        return None


def run(period="5y", require_taper=True, conditioned=False):
    symbols = DEFAULT_TICKERS + [BENCHMARK]
    raw = yf.download(symbols, period=period, interval="1d",
                      auto_adjust=True, progress=False, group_by="column")
    close_df = raw["Close"]
    vol_df   = raw["Volume"]
    reg = _regime_labels_for(close_df.index) if conditioned else None
    per_symbol, agg = {}, defaultdict(list)
    for t in symbols:
        if t not in close_df.columns:
            continue
        c = close_df[t].to_numpy(dtype=float)
        v = vol_df[t].to_numpy(dtype=float) if t in vol_df.columns else np.full(len(c), np.nan)
        ev = _detect(c, v, require_taper)
        if reg is not None:                            # keep only regime-aligned events
            lab = reg.to_numpy()
            ev = [e for e in ev if _regime_ok(e[0], lab[e[1]])]
        per_symbol[t] = {"bull": _summary(ev, "bull"), "bear": _summary(ev, "bear")}
        agg["all"].extend(ev)
    overall = {"bull": _summary(agg["all"], "bull"), "bear": _summary(agg["all"], "bear")}
    return {"period": period, "taper": require_taper, "success_horizon": SUCCESS_H,
            "conditioned": bool(reg is not None), "per_symbol": per_symbol, "overall": overall}


def _print(report):
    th = SUCCESS_H
    taper = "with volume taper" if report["taper"] else "no taper filter"
    cond = (" · REGIME-CONDITIONED (bull outside / bear inside DETERIORATING)"
            if report.get("conditioned") else "")
    print(f"\nFlag success-rate study — {report['period']} daily, {taper}, "
          f"continuation measured at +{th} bars{cond}\n")
    print(f"{'Symbol':<7}{'Bull n':>7}{'Bull win%':>10}{'Bull avg%':>10}"
          f"{'Bear n':>8}{'Bear win%':>10}{'Bear avg%':>10}")
    for t, d in report["per_symbol"].items():
        b, r = d["bull"]["horizons"].get(th, {}), d["bear"]["horizons"].get(th, {})
        print(f"{t:<7}{b.get('n', 0):>7}{b.get('success_rate', '—'):>10}{b.get('avg_move', '—'):>10}"
              f"{r.get('n', 0):>8}{r.get('success_rate', '—'):>10}{r.get('avg_move', '—'):>10}")
    o = report["overall"]
    ob, orr = o["bull"]["horizons"].get(th, {}), o["bear"]["horizons"].get(th, {})
    print("-" * 62)
    print(f"{'ALL':<7}{ob.get('n', 0):>7}{ob.get('success_rate', '—'):>10}{ob.get('avg_move', '—'):>10}"
          f"{orr.get('n', 0):>8}{orr.get('success_rate', '—'):>10}{orr.get('avg_move', '—'):>10}")
    print("\nWin% = share that continued in the flagpole's direction; avg% = mean "
          f"forward move at +{th} bars. A working pattern shows win% > 50.\n")


def main():
    ap = argparse.ArgumentParser(description="Bull/bear flag success-rate study")
    ap.add_argument("--period", default="5y")
    ap.add_argument("--no-taper", action="store_true", help="drop the volume-taper filter")
    ap.add_argument("--regime", action="store_true",
                    help="condition on the breadth regime (bull outside / bear inside "
                         "DETERIORATING) — the honest bear-flag edge")
    args = ap.parse_args()
    report = run(period=args.period, require_taper=not args.no_taper,
                 conditioned=args.regime)
    _print(report)
    if args.regime and not report["conditioned"]:
        print("NOTE: no breadth data found — ran UNCONDITIONED. Sync breadth (sp500) "
              "first for the regime-conditioned edge.\n")


if __name__ == "__main__":
    main()
