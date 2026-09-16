#!/usr/bin/env python3
"""Planned vs adhoc breakdown for a cycle, its carry-over, and its planning stamp.

Answers the questions worth asking at a cycle boundary: how much of the week
went to unplanned work, how much rolled forward unfinished, and whether the
cycle was ever planned. The numbers come from cycles.py; this only prints.

    python3 cycle-report.py            # the active cycle
    python3 cycle-report.py --cycle 1
    python3 cycle-report.py --previous
    python3 cycle-report.py --previous --as-of 2026-08-16
    python3 cycle-report.py --json
    python3 cycle-report.py --mark-unplanned --note "focus set ad hoc"   # dry run
    python3 cycle-report.py --mark-planned --cycle 9 --yes               # writes
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cycles import (  # noqa: E402
    DESCRIPTION_MAX,
    cycle_phase,
    days_until,
    fetch_cycles,
    fetch_members,
    fetch_uncompleted_upon_close,
    mark_planning,
    parse_ts,
    previous_cycle,
    select_cycle,
    set_planning,
    summarize,
)


def bar(part: int, whole: int, width: int = 24) -> str:
    if not whole:
        return " " * width
    filled = round(width * part / whole)
    return "█" * filled + "·" * (width - filled)


def as_of(s: str) -> datetime.datetime:
    """A bare date is local midnight; a naive datetime is local time."""
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date or datetime: {s!r}") from None
    return dt if dt.tzinfo else dt.astimezone()


def mark(cycle: dict, status: str, note: str | None, yes: bool) -> int:
    """Show the exact before/after, and write only with --yes."""
    before = cycle["description"] or ""
    try:
        after = set_planning(before, status, note)
    except ValueError as e:
        sys.exit(f"cycle {cycle['number']}: {e}")
    print(f"cycle {cycle['number']} description")
    print(f"  before ({len(before)}/{DESCRIPTION_MAX} chars): {cycle['description']!r}")
    print(f"  after  ({len(after)}/{DESCRIPTION_MAX} chars): {after!r}")
    if after == before:
        print("unchanged — nothing to write")
        return 0
    if not yes:
        print("dry run — nothing written; re-run with --yes to write")
        return 0
    result = mark_planning(cycle["id"], after)
    if not result.get("success"):
        sys.exit(f"cycleUpdate did not succeed for cycle {cycle['number']}")
    written = result["cycle"]
    print(f"written: cycle {written['number']} description is now {written['description']!r}")
    return 0


def report(s: dict, now: datetime.datetime) -> None:
    c = s["cycle"]
    n = c["number"]
    start = parse_ts(c["starts_at"]).astimezone(now.tzinfo).date()
    end = parse_ts(c["ends_at"]).astimezone(now.tzinfo).date()
    if c["state"] == "ended":
        when = "ended"
    elif c["state"] == "active":
        when = f"{c['days_left']}d left"
    else:
        when = f"starts in {days_until(c['starts_at'], now)}d"
    print(f"\nCycle {n}   {start} → {end}   ({when})")
    print("=" * 62)

    planning = s["planning"]
    if planning["status"] is None:
        print(f"  ⚠ cycle {n} has no planning stamp — never planned?"
              " (mark with --mark-planned / --mark-unplanned)")
    elif planning["status"] == "skipped":
        print(f"  ⚠ cycle {n} was not planned" + (f": {planning['note']}" if planning["note"] else ""))

    t = s["totals"]
    if t["source"] == "history":
        print("  totals from Linear history at close; rows use current labels and estimates")
    elif c["state"] == "ended" and not c["closed"]:
        print("  ended but not yet closed by Linear — totals are current membership")
    else:
        print("  totals from current membership")

    for name in ("planned", "adhoc"):
        r = s["split"][name]
        i, p = r["issues"], r["points"]
        print(
            f"  {name:<8} {i['done']:>2}/{i['scope']:<3} issues   "
            f"{p['done']:>3}/{p['scope']:<3} pts   {bar(i['done'], i['scope'])}"
        )

    i, p = t["issues"], t["points"]
    print("-" * 62)
    print(f"  {'total':<8} {i['done']:>2}/{i['scope']:<3} issues   {p['done']:>3}/{p['scope']:<3} pts")

    co = s["carry_out"]
    if co:
        print(f"\n  carry-out: {co['issues']} issues / {co['points']} pts unfinished at close")
        print(f"      planned {co['planned']['issues']} issues / {co['planned']['points']} pts, "
              f"adhoc {co['adhoc']['issues']} issues / {co['adhoc']['points']} pts")
        if co["points_current_estimates"] != co["points"]:
            print(f"      (split uses current labels and estimates: {co['points_current_estimates']} pts"
                  f" today vs {co['points']} at close)")

    ci = s["carry_in"]
    if ci:
        print(f"\n  carry-in from cycle {ci['from_cycle']}: {ci['issues']} issues / {ci['points']} pts"
              f" ({ci['share_of_members'] or 0}% of members)")
        print(f"      planned {ci['planned']['issues']} issues / {ci['planned']['points']} pts, "
              f"adhoc {ci['adhoc']['issues']} issues / {ci['adhoc']['points']} pts")

    share = s["adhoc_share"]
    population = s["split"]["planned"]["issues"]["scope"] + s["split"]["adhoc"]["issues"]["scope"]
    if share["issues"] is not None:
        print(f"\n  adhoc share: {share['issues']}% of issues", end="")
        print(f", {share['points']}% of points" if share["points"] is not None else "")
        print(f"  created mid-cycle: {s['created_mid_cycle']} of {population}")

    if s["delivered_by"]:
        print("\n  delivered by:")
        for name, row in s["delivered_by"].items():
            print(f"      {name:<17} {row['issues']:>2} issues  {row['points']:>3} pts  {row['pct'] or 0:>3}%")
        if "unlabelled" in s["delivered_by"]:
            print("      ⚠ unlabelled work predates the driven group, or was filed"
                  " outside linear.sh new")

    unest = s["unestimated"]
    if unest:
        print(f"\n  ⚠ {len(unest)} issue(s) with no estimate — point totals understate the work:")
        for u in unest[:8]:
            tag = "adhoc" if u["adhoc"] else "planned"
            print(f"      {u['identifier']:<8} [{tag}] {u['title'][:44]}")
        if len(unest) > 8:
            print(f"      … and {len(unest) - 8} more")

    if share["issues"] is not None:
        print("\n  → plan the next cycle at roughly "
              f"{max(0, 100 - share['issues'])}% of capacity; adhoc took the rest.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycle", type=int, help="cycle number (default: active)")
    ap.add_argument("--previous", action="store_true", help="the most recently ended cycle")
    ap.add_argument("--as-of", type=as_of, metavar="ISO",
                    help="evaluate as if now were this date/datetime (naive = local time)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print the summary dict instead of the report")
    mode.add_argument("--mark-planned", action="store_true", help="stamp 'Planning: planned' on the cycle")
    mode.add_argument("--mark-unplanned", action="store_true", help="stamp 'Planning: skipped' on the cycle")
    ap.add_argument("--note", help="note for the planning stamp ('Planning: <status> -- <note>')")
    ap.add_argument("--yes", action="store_true", help="actually write the stamp (default: dry run)")
    args = ap.parse_args()

    marking = args.mark_planned or args.mark_unplanned
    if (args.note is not None or args.yes) and not marking:
        ap.error("--note and --yes only apply with --mark-planned / --mark-unplanned")

    now = args.as_of or datetime.datetime.now().astimezone()
    cycles = fetch_cycles()
    try:
        cycle = select_cycle(cycles, number=args.cycle, previous=args.previous, now=now)
    except LookupError as e:
        sys.exit(str(e))

    if marking:
        return mark(cycle, "planned" if args.mark_planned else "skipped", args.note, args.yes)

    # One extra query at most: a closed cycle needs its own carry-out set, an
    # unclosed one its closed predecessor's (for carry-in). Closed is Linear's
    # completedAt, not the clock: before the close job runs the set is empty.
    closed = bool(cycle.get("completedAt"))
    # Warn only when --as-of predates the cycle's end: at or after endsAt it was
    # genuinely over at that instant. endsAt, not completedAt, so the close
    # job's few seconds of lag on a boundary-day --as-of stay quiet.
    if closed and args.as_of and now < parse_ts(cycle["endsAt"]):
        print(f"warn: cycle {cycle['number']} is closed in Linear; showing close data,"
              " --as-of affects selection only", file=sys.stderr)
    members = fetch_members(cycle["id"])
    uncompleted = fetch_uncompleted_upon_close(cycle["id"]) if closed else None
    prev = None
    if not closed and cycle_phase(cycle, now) != "future":
        prev = previous_cycle(cycles, cycle)
    prev_uncompleted = fetch_uncompleted_upon_close(prev["id"]) if prev and prev.get("completedAt") else None
    summary = summarize(cycle, members, uncompleted, prev_uncompleted, prev_cycle=prev, now=now)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        report(summary, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
