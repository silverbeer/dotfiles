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
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from linear_api import gql, warn_if_capped  # noqa: E402

VALUES = ("human", "agent-supervised", "agent-auto")
# Label name → id cache (SB-923). Labels change rarely; the lookup is one full
# page every call otherwise. Bypass with LINEAR_CRUD_NO_CACHE=1.
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "linear-crud" / "labels.json"
CACHE_TTL = 3600


def fetch_labels() -> dict[str, str]:
    labels = gql('{ issueLabels(first: 250) { nodes { id name } } }')["issueLabels"]["nodes"]
    warn_if_capped(labels, 250, "issue labels")
    return {n["name"]: n["id"] for n in labels}


def label_ids(need: str) -> dict[str, str]:
    """Name → id map that is guaranteed fresh if `need` is absent from it.

    A stale cache can only ever produce a miss (a label that exists but isn't
    listed) — it can't map a name to a wrong id, since Linear never reuses
    them — so a miss simply refetches and rewrites."""
    if os.environ.get("LINEAR_CRUD_NO_CACHE") == "1":
        return fetch_labels()
    try:
        if time.time() - CACHE.stat().st_mtime < CACHE_TTL:
            cached = json.loads(CACHE.read_text())
            if isinstance(cached, dict) and need in cached:
                return cached
    except (OSError, ValueError):
        pass
    fresh = fetch_labels()
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=CACHE.parent, delete=False) as tmp:
            tmp.write(json.dumps(fresh))
        os.replace(tmp.name, CACHE)
    except OSError as e:
        print(f"warn: could not write label cache {CACHE}: {e}", file=sys.stderr)
    return fresh


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
    by_name = label_ids(target)
    if target not in by_name:
        sys.exit(f"set-driven: label '{target}' does not exist — create the driven group first")

    keep = [n["id"] for n in issue["labels"]["nodes"] if not n["name"].startswith("driven:")]

    m = """
    mutation($id: String!, $labelIds: [String!]!) {
      issueUpdate(id: $id, input: {labelIds: $labelIds}) {
        success issue { identifier labels { nodes { name } } }
      }
    }
    """
    def update(label_id: str) -> dict:
        ids = keep + [label_id]
        return gql(m, {"id": issue["id"], "labelIds": ids})["issueUpdate"]

    # A cached id can go stale if the label was deleted and re-created with
    # the same name. Linear reports that as an entity-not-found error; refetch
    # (bypassing the cache) and retry exactly once.
    try:
        res = update(by_name[target])
    except SystemExit as e:
        msg = str(e)
        if "not found" not in msg and "Entity" not in msg:
            raise
        print(f"warn: {msg} — refetching labels and retrying once", file=sys.stderr)
        by_name = fetch_labels()
        if target not in by_name:
            sys.exit(f"set-driven: label '{target}' does not exist — create the driven group first")
        res = update(by_name[target])
    if not res["success"]:
        sys.exit("set-driven: issueUpdate returned success=false")
    was = current[0] if current else "(none)"
    print(f"{res['issue']['identifier']}: {was} → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
