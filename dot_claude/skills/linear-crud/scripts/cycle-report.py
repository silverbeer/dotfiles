#!/usr/bin/env python3
"""Planned vs adhoc breakdown for a cycle.

Answers the two questions worth asking at a cycle boundary: how much of the
week went to unplanned work, and did any backlog actually get chipped away.

    python3 cycle-report.py            # the active cycle
    python3 cycle-report.py --cycle 1
    python3 cycle-report.py --previous
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from linear_api import gql, warn_if_capped  # noqa: E402
ADHOC = "adhoc"
DRIVEN_PREFIX = "driven:"
# Ordered worst-to-best on the autonomy climb; progress is the distribution
# moving down this list over cycles, which is why it is three values and not a
# human/agent boolean.
DRIVEN_ORDER = ["human", "agent-supervised", "agent-auto"]

# Two queries on purpose: cycles-with-issues in one shot blows Linear's
# complexity budget (~12k against a 10k ceiling).
Q_CYCLES = """
{ cycles(filter:{team:{key:{eq:"SB"}}}, first:50){
    nodes{ id number startsAt endsAt } } }
"""

Q_ISSUES = """
{ issues(filter:{cycle:{id:{eq:"%s"}}}, first:250){
    nodes{ identifier title estimate createdAt
           state{name type} labels{nodes{name}} } } }
"""


def bar(part: int, whole: int, width: int = 24) -> str:
    if not whole:
        return " " * width
    filled = round(width * part / whole)
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycle", type=int, help="cycle number (default: active)")
    ap.add_argument("--previous", action="store_true", help="the most recently ended cycle")
    args = ap.parse_args()

    today = datetime.date.today()
    cycles = gql(Q_CYCLES)["cycles"]["nodes"]
    warn_if_capped(cycles, 50, "cycles")
    if not cycles:
        sys.exit("no cycles found")

    def window(c):
        return (
            datetime.date.fromisoformat(c["startsAt"][:10]),
            datetime.date.fromisoformat(c["endsAt"][:10]),
        )

    if args.cycle is not None:
        cycle = next((c for c in cycles if c["number"] == args.cycle), None)
        if cycle is None:
            sys.exit(f"cycle {args.cycle} not found")
    elif args.previous:
        ended = [c for c in cycles if window(c)[1] < today]
        cycle = max(ended, key=lambda c: window(c)[1]) if ended else sys.exit("no ended cycle")
    else:
        active = [c for c in cycles if window(c)[0] <= today <= window(c)[1]]
        cycle = active[0] if active else max(cycles, key=lambda c: window(c)[1])

    start, end = window(cycle)
    issues = gql(Q_ISSUES % cycle["id"])["issues"]["nodes"]
    warn_if_capped(issues, 250, f"cycle {cycle['number']} issues")

    def is_adhoc(i) -> bool:
        return ADHOC in {n["name"] for n in i["labels"]["nodes"]}

    def driven(i) -> str:
        """Autonomy of delivery (SB-507). Orthogonal to planned/adhoc — an adhoc
        ticket can perfectly well be agent-delivered, so the two are reported as
        separate slices and never merged."""
        for n in i["labels"]["nodes"]:
            if n["name"].startswith(DRIVEN_PREFIX):
                return n["name"][len(DRIVEN_PREFIX):]
        return "unlabelled"

    def done(i) -> bool:
        return i["state"]["type"] == "completed"

    def pts(rows) -> int:
        return sum(i["estimate"] or 0 for i in rows)

    planned = [i for i in issues if not is_adhoc(i)]
    adhoc = [i for i in issues if is_adhoc(i)]
    born_in_cycle = [
        i for i in issues if start <= datetime.date.fromisoformat(i["createdAt"][:10]) <= end
    ]

    days_left = (end - today).days
    print(f"\nCycle {cycle['number']}   {start} → {end}", end="")
    print(f"   ({days_left}d left)" if days_left >= 0 else "   (ended)")
    print("=" * 62)

    for name, rows in (("planned", planned), ("adhoc", adhoc)):
        d = [i for i in rows if done(i)]
        print(
            f"  {name:<8} {len(d):>2}/{len(rows):<3} issues   "
            f"{pts(d):>3}/{pts(rows):<3} pts   {bar(len(d), len(rows))}"
        )

    total_done = [i for i in issues if done(i)]
    print("-" * 62)
    print(f"  {'total':<8} {len(total_done):>2}/{len(issues):<3} issues   {pts(total_done):>3}/{pts(issues):<3} pts")

    if issues:
        share = 100 * len(adhoc) / len(issues)
        print(f"\n  adhoc share: {share:.0f}% of issues", end="")
        if pts(issues):
            print(f", {100 * pts(adhoc) / pts(issues):.0f}% of points")
        else:
            print()
        print(f"  created mid-cycle: {len(born_in_cycle)} of {len(issues)}")

    # Autonomy slice (SB-507). Reported over COMPLETED work only: an unfinished
    # ticket has not been delivered by anyone yet, so counting its label would
    # credit an agent for work still in Todo.
    if total_done:
        counted = {d: [i for i in total_done if driven(i) == d] for d in DRIVEN_ORDER}
        counted["unlabelled"] = [i for i in total_done if driven(i) == "unlabelled"]
        shown = {k: v for k, v in counted.items() if v}
        if shown:
            print("\n  delivered by:")
            for name, rows in shown.items():
                pct = 100 * pts(rows) / pts(total_done) if pts(total_done) else 0
                print(f"      {name:<17} {len(rows):>2} issues  {pts(rows):>3} pts  {pct:>3.0f}%")
            if counted["unlabelled"]:
                print("      ⚠ unlabelled work predates the driven group, or was filed"
                      " outside linear.sh new")

    # Estimates are the weak spot — unestimated work makes points meaningless.
    # `is None`, not falsy: 0 is a deliberate estimate meaning "closed as
    # superseded/duplicate, no work done", and must not be flagged as missing.
    unest = [i for i in issues if i["estimate"] is None]
    if unest:
        print(f"\n  ⚠ {len(unest)} issue(s) with no estimate — point totals understate the work:")
        for i in unest[:8]:
            tag = "adhoc" if is_adhoc(i) else "planned"
            print(f"      {i['identifier']:<8} [{tag}] {i['title'][:44]}")
        if len(unest) > 8:
            print(f"      … and {len(unest) - 8} more")

    if issues:
        print("\n  → plan the next cycle at roughly "
              f"{max(0, 100 - round(share)):.0f}% of capacity; adhoc took the rest.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
