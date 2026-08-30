#!/usr/bin/env python3
"""Write an approved groom change set to Linear. The only step that mutates.

Idempotent: re-reads each issue first and skips fields that already match, so
running it twice is a no-op and a partial run can simply be re-run.

    python3 apply.py --changes apply.json            # dry run, prints the plan
    python3 apply.py --changes apply.json --confirm   # actually writes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# linear_api.py lives in the linear-crud skill. Resolve it from, in order:
# LINEAR_CRUD_SCRIPTS (tests point this at a scratch copy), the sibling skill
# in the chezmoi source tree, then the deployed ~/.claude copy.
_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "linear-crud" / "scripts",
    Path.home() / ".claude/skills/linear-crud/scripts",
]
if os.environ.get("LINEAR_CRUD_SCRIPTS"):
    _CANDIDATES.insert(0, Path(os.environ["LINEAR_CRUD_SCRIPTS"]))
LINEAR_CRUD = next((p for p in _CANDIDATES if (p / "linear_api.py").is_file()), _CANDIDATES[-1])
sys.path.insert(0, str(LINEAR_CRUD))
try:
    from linear_api import gql, warn_if_capped
except ImportError:
    sys.exit(f"missing {LINEAR_CRUD}/linear_api.py — the linear-crud skill must be installed")

FIELDS = ("priority", "project", "estimate")


def current_state(ids: list[str]) -> dict[str, dict]:
    numbers = ",".join(i.split("-")[1] for i in ids)
    data = gql(
        '{ issues(filter:{number:{in:[%s]},team:{key:{eq:"SB"}}}, first:250){ nodes{'
        " id identifier priority estimate project{id name} } } }" % numbers
    )
    nodes = data["issues"]["nodes"]
    warn_if_capped(nodes, 250, "issues to update")
    return {n["identifier"]: n for n in nodes}


def projects() -> dict[str, str]:
    data = gql("{ projects(first:100){ nodes{ id name } } }")
    nodes = data["projects"]["nodes"]
    warn_if_capped(nodes, 100, "projects")
    return {n["name"]: n["id"] for n in nodes}


def plan(changes: list[dict], live: dict[str, dict], known_projects: dict[str, str]) -> tuple[list, int]:
    """The idempotence step, kept pure so it can be tested without Linear.

    Returns (planned, skipped): planned is [(change, current, fields)] with
    only the fields that differ from what is live; skipped counts changes
    that already match. Re-running on the post-apply state plans nothing."""
    planned, skipped = [], 0
    for c in changes:
        cur = live[c["id"]]
        fields = {}
        if "priority" in c and cur["priority"] != c["priority"]:
            fields["priority"] = c["priority"]
        if "estimate" in c and cur["estimate"] != c["estimate"]:
            fields["estimate"] = c["estimate"]
        if "project" in c and (cur["project"] or {}).get("name") != c["project"]:
            fields["projectId"] = known_projects[c["project"]]
        if fields:
            planned.append((c, cur, fields))
        else:
            skipped += 1
    return planned, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--changes", required=True)
    ap.add_argument("--confirm", action="store_true", help="required to write")
    args = ap.parse_args()

    changes = json.loads(Path(args.changes).read_text())["changes"]
    if not changes:
        print("nothing to apply")
        return 0

    live = current_state([c["id"] for c in changes])
    known_projects = projects()

    missing = [c["id"] for c in changes if c["id"] not in live]
    if missing:
        sys.exit(f"REFUSING: not found in Linear: {', '.join(missing)}")

    unknown_epics = sorted(
        {c["project"] for c in changes if c.get("project") and c["project"] not in known_projects}
    )
    if unknown_epics:
        sys.exit(
            "REFUSING: these epics do not exist in Linear yet — create them first "
            "(and add an epicGlobs entry for each to linear-crud/repos.json):\n  "
            + "\n  ".join(unknown_epics)
        )

    planned, skipped = plan(changes, live, known_projects)

    for c, _, fields in planned:
        shown = {k: v for k, v in fields.items() if k != "projectId"}
        if "projectId" in fields:
            shown["project"] = c["project"]
        print(f"  {c['id']:<8} {shown}  — {c['title'][:56]}")
    print(f"\n{len(planned)} to update, {skipped} already correct")

    if not args.confirm:
        print("\ndry run — pass --confirm to write")
        return 0

    written = 0
    for c, _, fields in planned:
        parts = [f"{k}: {json.dumps(v)}" for k, v in fields.items()]
        gql(
            'mutation { issueUpdate(id: "%s", input: { %s }) { success } }'
            % (live[c["id"]]["id"], ", ".join(parts))
        )
        written += 1
        print(f"  updated {c['id']}")
    print(f"\n{written} issues updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
