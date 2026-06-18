"""
Harness CLI — print today's market brief from the terminal.

    /usr/bin/python3 -m modules.harness.cli              # deterministic brief, no LLM ($0)
    /usr/bin/python3 -m modules.harness.cli --llm         # add Claude narration (subscription)
    /usr/bin/python3 -m modules.harness.cli --json        # raw payload
    /usr/bin/python3 -m modules.harness.cli --backtest    # the referee: A/B harness vs RRG vs beta
    /usr/bin/python3 -m modules.harness.cli --import-watchlist PATH  # load a TradingView export
    /usr/bin/python3 -m modules.harness.cli --picks       # ranked impulse×hold suggestions

Same code path as GET /api/harness. No-LLM by default so it's free and fast.
"""

import json
import sys

from modules.harness import build_brief


def _arg_value(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


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

    path = _arg_value(argv, "--import-watchlist")
    if path:
        from modules.harness import watchlist
        with open(path) as f:
            res = watchlist.import_text(f.read())
        print(f"  imported {res['parsed']} tickers → watchlist now {res['count']}: "
              + ", ".join(res["symbols"][:30]) + (" …" if res["count"] > 30 else ""))
        return

    if "--picks" in argv:
        from modules.harness import picks
        rep = picks.suggest()
        if as_json:
            print(json.dumps(rep, indent=2, default=str)); return
        print()
        ctx = rep["ctx"]
        print(f"  WATCHLIST SUGGESTIONS — {rep['count']} names · regime {ctx['regime']}"
              f"{' · EVENT RISK' if ctx.get('event_risk') else ''} · as of {rep['as_of']}")
        print("  " + "-" * 72)
        if not rep["suggestions"]:
            print("  " + rep["note"]); print(); return
        print(f"  {'SYM':<7}{'PICK':>5}{'IMP':>5}{'HOLD':>5}  {'STOP':>8}  WHY")
        for s in rep["suggestions"]:
            tag = "" if s["tradeable"] else f"  [{s['reason']}]"
            stop = f"{s['stop']:.2f}" if s["stop"] is not None else "—"
            print(f"  {s['symbol']:<7}{s['pick']:>5.0f}{s['impulse']:>5.0f}{s['hold']:>5.0f}"
                  f"  {stop:>8}  {', '.join(s['why'][:3])}{tag}")
        print("  " + "-" * 72)
        print(f"  {rep['tradeable']} tradeable (impulse≥{int(picks.MIN_IMPULSE)} "
              f"& hold≥{int(picks.MIN_HOLD)}).  {rep['note']}")
        print()
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
