"""
Bull/bear flag success-rate study — standalone, PRICE + VOLUME.

The live conviction engine carries a flag *shape* factor on the RS line (which has
no volume). This script is the rigorous companion the user asked for: it detects
classic price flags — an impulsive **flagpole** (strong move) followed by a brief,
shallow consolidation on **tapering volume** (the flag) — on each SPDR sector ETF
+ SPY, then measures whether the continuation actually happens. It answers "how
often does a flag work, and by how much?" across the basket.

Detection is no-lookahead at each flag (uses only bars up to the flag); the
forward returns it scores are, of course, in the future — that's the study.

Run:  /usr/bin/python3 -m modules.rrg.flag_backtest [--no-taper] [--period 5y]
"""

import argparse
from collections import defaultdict

import numpy as np
import yfinance as yf

from .signal import DEFAULT_TICKERS, BENCHMARK

# --- pattern parameters (daily bars) ---
POLE_BARS     = 10     # window for the flagpole
FLAG_BARS     = 5      # window for the flag consolidation
POLE_MIN_RET  = 0.06   # flagpole must move ≥ this (fraction) over POLE_BARS
FLAG_MAX_RETR = 0.45   # flag may pull back at most this fraction of the pole
FLAG_MAX_RANGE= 0.5    # flag close-range ≤ this fraction of the pole (tightness)
FWD_HORIZONS  = (5, 10, 20)
SUCCESS_H     = 10     # horizon the headline success rate is measured at
COOLDOWN      = FLAG_BARS   # bars to wait before logging another flag on a symbol


def _detect(close, vol, require_taper):
    """Yield flag events for one symbol: (kind, index, {h: fwd_return})."""
    n = len(close)
    events = []
    last = -COOLDOWN - 1
    need = POLE_BARS + FLAG_BARS
    for e in range(need, n):
        if e - last < COOLDOWN:
            continue
        fs = e - FLAG_BARS + 1                       # flag start
        ps = fs - POLE_BARS                          # pole start
        pole_a, pole_b = close[ps], close[fs]        # pole endpoints
        if not (np.isfinite(pole_a) and pole_a > 0 and np.isfinite(pole_b)):
            continue
        pole_ret = (pole_b - pole_a) / pole_a
        pole_abs = abs(pole_b - pole_a)
        if pole_abs <= 0:
            continue
        flag = close[fs:e + 1]
        if not np.all(np.isfinite(flag)):
            continue
        flag_range = (np.max(flag) - np.min(flag)) / pole_abs
        # volume taper: mean volume in the flag below the pole's
        taper = True
        if require_taper:
            pv, fv = np.nanmean(vol[ps:fs + 1]), np.nanmean(vol[fs:e + 1])
            taper = np.isfinite(pv) and np.isfinite(fv) and pv > 0 and fv < pv

        kind = None
        if pole_ret >= POLE_MIN_RET:                 # bull flag
            retr = (pole_b - close[e]) / pole_abs     # pullback off the pole top
            if 0.0 <= retr <= FLAG_MAX_RETR and flag_range <= FLAG_MAX_RANGE and close[e] <= pole_b and taper:
                kind = "bull"
        elif pole_ret <= -POLE_MIN_RET:              # bear flag
            retr = (close[e] - pole_b) / pole_abs
            if 0.0 <= retr <= FLAG_MAX_RETR and flag_range <= FLAG_MAX_RANGE and close[e] >= pole_b and taper:
                kind = "bear"
        if kind is None:
            continue

        fwd = {}
        for h in FWD_HORIZONS:
            if e + h < n and np.isfinite(close[e + h]) and close[e] > 0:
                fwd[h] = (close[e + h] / close[e] - 1) * 100
        events.append((kind, e, fwd))
        last = e
    return events


def _summary(events, kind):
    """Stats for one kind ('bull'/'bear'). Success = move continued in the pole's
    direction (bull → up, bear → down) at SUCCESS_H."""
    rows = [ev for ev in events if ev[0] == kind]
    out = {"n": len(rows), "horizons": {}}
    for h in FWD_HORIZONS:
        r = np.array([ev[2][h] for ev in rows if h in ev[2]], dtype=float)
        if not r.size:
            continue
        cont = (r > 0) if kind == "bull" else (r < 0)     # continuation in pole direction
        out["horizons"][h] = {
            "n": int(r.size),
            "success_rate": round(100.0 * cont.mean(), 1),
            "avg_move": round(float(r.mean()), 2),
            "median_move": round(float(np.median(r)), 2),
        }
    return out


def run(period="5y", require_taper=True):
    symbols = DEFAULT_TICKERS + [BENCHMARK]
    raw = yf.download(symbols, period=period, interval="1d",
                      auto_adjust=True, progress=False, group_by="column")
    close_df = raw["Close"]
    vol_df   = raw["Volume"]
    per_symbol, agg = {}, defaultdict(list)
    for t in symbols:
        if t not in close_df.columns:
            continue
        c = close_df[t].to_numpy(dtype=float)
        v = vol_df[t].to_numpy(dtype=float) if t in vol_df.columns else np.full(len(c), np.nan)
        ev = _detect(c, v, require_taper)
        per_symbol[t] = {"bull": _summary(ev, "bull"), "bear": _summary(ev, "bear")}
        agg["all"].extend(ev)
    overall = {"bull": _summary(agg["all"], "bull"), "bear": _summary(agg["all"], "bear")}
    return {"period": period, "taper": require_taper, "success_horizon": SUCCESS_H,
            "per_symbol": per_symbol, "overall": overall}


def _print(report):
    th = SUCCESS_H
    taper = "with volume taper" if report["taper"] else "no taper filter"
    print(f"\nFlag success-rate study — {report['period']} daily, {taper}, "
          f"continuation measured at +{th} bars\n")
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
    args = ap.parse_args()
    _print(run(period=args.period, require_taper=not args.no_taper))


if __name__ == "__main__":
    main()
