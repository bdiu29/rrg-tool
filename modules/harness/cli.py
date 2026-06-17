"""
Harness CLI — print today's market brief from the terminal.

    /usr/bin/python3 -m modules.harness.cli              # deterministic brief, no LLM ($0)
    /usr/bin/python3 -m modules.harness.cli --llm         # add Claude narration (subscription)
    /usr/bin/python3 -m modules.harness.cli --json        # raw payload
    /usr/bin/python3 -m modules.harness.cli --backtest    # the referee: A/B harness vs RRG vs beta

Same code path as GET /api/harness. No-LLM by default so it's free and fast.
"""

import json
import sys

from modules.harness import build_brief


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    use_llm  = "--llm" in argv
    as_json  = "--json" in argv

    if "--backtest" in argv:
        from modules.harness import backtest
        rep = backtest.run_harness_backtest()
        print(json.dumps(rep, indent=2, default=str) if as_json
              else backtest.format_report(rep))
        return

    payload = build_brief(llm=use_llm)

    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return

    c = payload["combined"]
    print()
    print(f"  HARNESS BRIEF — {payload['date']}")
    print(f"  Stance: {c['stance']}   composite {c['score']:+.0f} ({c['posture']})")
    print(f"  regime {c.get('regime') or '—'}   rotation {c.get('rotation') or '—'}   "
          f"votes {c['n_votes']}")
    print("  " + "-" * 58)

    for v in payload["votes"]:
        arrow = {1: "▲", -1: "▼", 0: "·"}[v["direction"]]
        ok    = "" if v["ok"] else "  (no data)"
        print(f"  {arrow} {v['domain']:<9} dir {v['direction']:+d}  "
              f"conv {v['conviction']:>5}  w{v['weight']:<3}{ok}")
        if v.get("rationale"):
            print(f"      {v['rationale']}")

    if c["longs"]:
        print("  " + "-" * 58)
        print("  Confluence longs : " + ", ".join(
            f"{x['ticker']}({x['call']})" for x in c["longs"]))
    if c["avoids"]:
        print("  Avoids           : " + ", ".join(
            f"{x['ticker']}({x['call']})" for x in c["avoids"]))

    print("  " + "-" * 58)
    print(payload["brief"])
    print()


if __name__ == "__main__":
    main()
