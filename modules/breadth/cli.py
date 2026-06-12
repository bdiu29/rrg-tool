"""
Daily breadth summary printout.

Usage:
    /usr/bin/python3 -m modules.breadth.cli [universe] [--json]

Prints the current regime, active divergence flags, and any short-term
extremes — same code path as GET /api/breadth/summary.
"""

import json
import sys

from . import DEFAULT_UNIVERSE, build_summary


def main(argv=None):
    args     = list(argv if argv is not None else sys.argv[1:])
    as_json  = "--json" in args
    args     = [a for a in args if not a.startswith("--")]
    universe = args[0] if args else DEFAULT_UNIVERSE

    s = build_summary(universe)
    if as_json:
        print(json.dumps(s, indent=2))
        return

    print()
    print(f"  MARKET BREADTH — {s['name']}")
    if not s.get("regime"):
        print(f"  {s.get('note', 'no data')}")
        print()
        return
    print(f"  data through {s['as_of']}")
    print()
    print(f"  Regime: {s['regime']}  (score {s['score']:+d})")
    for r in s["reasons"]:
        print(f"    · {r}")
    print()
    m = s["metrics"]
    print(f"  McClellan {m['mcclellan']:+.0f}   Summation {m['summation']:+.0f}   "
          f"TRIN {m['trin']:.2f}   NetAdv {m['net_advances']:+.0f}   NH-NL {m['nh_nl']:+.0f}")
    print(f"  above MA: 20d {m['pct_above_20']:.0f}%   50d {m['pct_above_50']:.0f}%   "
          f"200d {m['pct_above_200']:.0f}%")
    print()
    print("  Read:")
    for line in s["interpretation"]:
        print(f"    · {line}")
    if s["active_divergences"]:
        print()
        print("  Active divergences:")
        for e in s["active_divergences"]:
            print(f"    ⚑ {e['date']} {e['kind']}: {e['detail']}")
    print()
    print(f"  ({s['note']})")
    print()


if __name__ == "__main__":
    main()
