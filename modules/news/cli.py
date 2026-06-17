"""
Upcoming macro-events printout.

Usage:
    /usr/bin/python3 -m modules.news.cli [--days N] [--high] [--json]

Same code path as GET /api/news/calendar — refreshes the sources, then prints
the upcoming economic calendar / FOMC / Fed events (and the next event-risk).
"""

import json
import sys

from . import calendar, store


def main(argv=None):
    args    = list(argv if argv is not None else sys.argv[1:])
    as_json = "--json" in args
    high    = "--high" in args
    days    = calendar.WINDOW_AHEAD
    if "--days" in args:
        i = args.index("--days")
        if i + 1 < len(args):
            try:
                days = int(args[i + 1])
            except ValueError:
                pass

    store.init_db()
    data = calendar.build_calendar(days_ahead=days,
                                   importance=(["high"] if high else None))
    if as_json:
        print(json.dumps(data, indent=2))
        return

    er = calendar.event_risk()
    print()
    print(f"  MACRO EVENTS — as of {data['as_of']}")
    if er.get("event"):
        print(f"  ⚑ {er['note']}")
    print()
    up = data["upcoming"]
    if not up:
        print("  (no upcoming events in range)")
    for ev in up:
        imp = ev["importance"].upper().ljust(4)
        print(f"  {ev['event_date']}  {ev['when']:>9}  [{imp}]  {ev['title']}")
    print()
    print(f"  ({data['note']})")
    print()


if __name__ == "__main__":
    main()
