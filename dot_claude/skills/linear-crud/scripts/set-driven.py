#!/usr/bin/env python3
"""Re-stamp an issue's `driven:*` autonomy label (SB-507).

Called by `linear.sh driven SB-N <value>`. Not meant to be run directly, but it
works standalone:

    LINEAR_ISSUE_NUM=507 LINEAR_DRIVEN_VALUE=agent-supervised python3 set-driven.py

`driven` is a mutually-exclusive label group, so adding a second child fails with
"labelIds not exclusive child labels". The fix is to rewrite the issue's whole
label set with any existing driven:* dropped — hence GraphQL rather than the CLI,
whose `-l` only ever appends.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

GQL = Path(__file__).with_name("linear-gql.sh")
VALUES = ("human", "agent-supervised", "agent-auto")


def gql(query: str, variables: dict | None = None) -> dict:
    cmd = ["bash", str(GQL), query]
    if variables is not None:
        cmd.append(json.dumps(variables))
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    payload = json.loads(out)
    if "errors" in payload:
        sys.exit(f"Linear API error: {payload['errors']}")
    return payload["data"]


def main() -> int:
    team = os.environ.get("LINEAR_TEAM", "SB")
    num = os.environ.get("LINEAR_ISSUE_NUM", "")
    value = os.environ.get("LINEAR_DRIVEN_VALUE", "")
    if not num or value not in VALUES:
        sys.exit(f"set-driven: need LINEAR_ISSUE_NUM and LINEAR_DRIVEN_VALUE in {VALUES}")

    q = """
    query($team: String!, $num: Float!) {
      issues(filter: {team: {key: {eq: $team}}, number: {eq: $num}}, first: 1) {
        nodes { id identifier labels { nodes { id name } } }
      }
    }
    """
    nodes = gql(q, {"team": team, "num": float(num)})["issues"]["nodes"]
    if not nodes:
        sys.exit(f"set-driven: {team}-{num} not found")
    issue = nodes[0]

    target = f"driven:{value}"
    current = [n["name"] for n in issue["labels"]["nodes"] if n["name"].startswith("driven:")]
    if current == [target]:
        print(f"{issue['identifier']}: already {target}")
        return 0

    # Look the label up by name rather than hardcoding a uuid — the ids differ
    # per workspace, and a stale constant here fails silently as a no-op.
    labels = gql(
        '{ issueLabels(first: 250) { nodes { id name } } }'
    )["issueLabels"]["nodes"]
    by_name = {n["name"]: n["id"] for n in labels}
    if target not in by_name:
        sys.exit(f"set-driven: label '{target}' does not exist — create the driven group first")

    keep = [n["id"] for n in issue["labels"]["nodes"] if not n["name"].startswith("driven:")]
    keep.append(by_name[target])

    m = """
    mutation($id: String!, $labelIds: [String!]!) {
      issueUpdate(id: $id, input: {labelIds: $labelIds}) {
        success issue { identifier labels { nodes { name } } }
      }
    }
    """
    res = gql(m, {"id": issue["id"], "labelIds": keep})["issueUpdate"]
    if not res["success"]:
        sys.exit("set-driven: issueUpdate returned success=false")
    was = current[0] if current else "(none)"
    print(f"{res['issue']['identifier']}: {was} → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
