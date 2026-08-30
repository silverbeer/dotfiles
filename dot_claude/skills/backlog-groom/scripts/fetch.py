#!/usr/bin/env python3
"""Fetch open Linear issues for grooming, as JSON on stdout.

Read-only. Auth is reused from linear-gql.sh, so the API key is never handled
here and never printed.

    python3 fetch.py --repo MT --repo MTA > groom-input.json
    python3 fetch.py --all > groom-input.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

LINEAR_CRUD = Path.home() / ".claude/skills/linear-crud/scripts"
sys.path.insert(0, str(LINEAR_CRUD))
try:
    from linear_api import gql, warn_if_capped
except ImportError:
    sys.exit(f"missing {LINEAR_CRUD}/linear_api.py — the linear-crud skill must be installed")

QUERY = """
{ issues(filter:{team:{key:{eq:"%s"}}, state:{type:{nin:["completed","canceled"]}}},
         first:250){
    nodes{ identifier title description priority estimate createdAt updatedAt
           state{name} project{name} labels{nodes{name}} }
} }
"""


def fetch(team: str) -> list[dict]:
    nodes = gql(QUERY % team)["issues"]["nodes"]
    warn_if_capped(nodes, 250, "open issues")
    return nodes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", action="append", default=[], help="repo label, repeatable (MT, STK…)")
    ap.add_argument("--all", action="store_true", help="every open issue, no repo filter")
    ap.add_argument("--team", default="SB")
    ap.add_argument("--today", help="YYYY-MM-DD, for reproducible ages (default: system date)")
    args = ap.parse_args()

    if not args.repo and not args.all:
        sys.exit("pass --repo (repeatable) or --all")

    today = (
        datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
    )
    wanted = set(args.repo)
    rows = []
    for i in fetch(args.team):
        labels = [n["name"] for n in i["labels"]["nodes"]]
        if wanted and not (wanted & set(labels)):
            continue
        created = datetime.date.fromisoformat(i["createdAt"][:10])
        rows.append(
            {
                "id": i["identifier"],
                "title": i["title"],
                # Descriptions can be long; the rubric only needs the gist.
                "description": (i["description"] or "")[:1200],
                "labels": labels,
                "state": i["state"]["name"],
                "project": (i["project"] or {}).get("name"),
                "priority": i["priority"] or 0,
                "estimate": i["estimate"],
                "age_days": (today - created).days,
                "created": i["createdAt"][:10],
            }
        )

    rows.sort(key=lambda r: -r["age_days"])
    json.dump({"generated_for": today.isoformat(), "count": len(rows), "issues": rows}, sys.stdout, indent=2)
    print(file=sys.stderr)
    print(f"{len(rows)} open issues", file=sys.stderr)
    for field in ("priority", "project", "estimate"):
        missing = sum(1 for r in rows if not r[field])
        print(f"  missing {field}: {missing}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
