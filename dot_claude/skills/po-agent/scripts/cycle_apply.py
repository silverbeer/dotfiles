#!/usr/bin/env python3
"""cycle_apply.py — write an approved /cycle change set to Linear (SB-1087).

The ONLY writer in the po-agent skill. Dry run by default: it re-reads every
issue and the cycle, prints the exact mutations it would send, and sends
none. `--confirm` sends them. Idempotent: fields that already match are
skipped, so running it twice is a no-op and a partial run can simply be re-run.

    python3 cycle_apply.py --changes changes.json             # dry run
    python3 cycle_apply.py --changes changes.json --confirm   # writes

changes.json:

    {"cycle": {"number": 9, "planning": "planned", "note": null},
     "issues": [
       {"identifier": "SB-1", "cycle": 9},
       {"identifier": "SB-2", "cycle": null},
       {"identifier": "SB-3", "priority": 2, "estimate": 3},
       {"identifier": "SB-4", "cancel": true}]}

Every key but `identifier` is optional; an absent key is left alone. An
unknown key, at any level, and a change set with no issues and no cycle stamp
are refused.
`"cycle": null` REMOVES the issue from its cycle — needed to drop carry-over,
because Linear rolls unfinished issues forward at close on its own. A cycle
change for an issue whose current cycle is still running (started, not yet
closed by Linear) is REFUSED: it would pull live work out of that cycle early
and rewrite its history. Apply it after the cycle closes, or pass
--allow-active-cycle-move when the user explicitly wants it moved now.
`"cancel": true` moves the issue to the team's Canceled state AND sets its
estimate to 0, so velocity never counts work nobody did. `cycle.planning`
("planned" or "skipped", or null for no stamp) is written last, after every
issue update succeeded.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

# linear-crud is a sibling skill: env override (tests), the chezmoi source
# tree, then the deployed ~/.claude copy — as cycle_state.py resolves it.
_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "linear-crud" / "scripts",
    Path.home() / ".claude/skills/linear-crud/scripts",
]
if os.environ.get("LINEAR_CRUD_SCRIPTS"):
    _CANDIDATES.insert(0, Path(os.environ["LINEAR_CRUD_SCRIPTS"]))
LINEAR_CRUD = next((p for p in _CANDIDATES if (p / "cycles.py").is_file()), _CANDIDATES[-1])
sys.path.insert(0, str(LINEAR_CRUD))
try:
    import cycles  # noqa: E402
    from linear_api import gql, warn_if_capped  # noqa: E402  (tests monkeypatch cycle_apply.gql)
except ImportError:
    sys.exit(f"cycle_apply: missing {LINEAR_CRUD}/cycles.py — the linear-crud skill must be installed")

ISSUE_KEYS = {"identifier", "cycle", "priority", "estimate", "cancel"}
TOP_KEYS = {"cycle", "issues"}
STAMP_KEYS = {"number", "planning", "note"}

Q_LIVE = """
query($n:[Float!]){ issues(filter:{team:{key:{eq:"SB"}}, number:{in:$n}}, first:250){
    nodes{ id identifier title priority estimate cycle{id number} state{id name type} } } }
"""

Q_CANCELED = """
{ workflowStates(filter:{team:{key:{eq:"SB"}}, type:{eq:"canceled"}}){ nodes{ id name } } }
"""

M_ISSUE = """
mutation($id:String!,$input:IssueUpdateInput!){ issueUpdate(id:$id, input:$input){ success } }
"""


def validate(changes: dict) -> None:
    """Refuse a malformed change set before anything is read or written."""
    if not isinstance(changes, dict):
        raise ValueError("the change set must be a JSON object")
    unknown = set(changes) - TOP_KEYS
    if unknown:
        raise ValueError(f"unknown top-level key(s) {sorted(unknown)} — only `cycle` and `issues`")
    issues = changes.get("issues", [])
    if not isinstance(issues, list):
        raise ValueError("`issues` must be a list")
    if not issues and changes.get("cycle") is None:
        raise ValueError("the change set is empty: no `issues` and no `cycle` stamp")
    seen = set()
    for c in issues:
        if not isinstance(c, dict):
            raise ValueError(f"each issue change must be an object, not {c!r}")
        k = c.get("identifier")
        if not isinstance(k, str) or not re.fullmatch(r"SB-\d+", k):
            raise ValueError(f"bad identifier: {k!r}")
        if k in seen:
            raise ValueError(f"{k} appears twice")
        seen.add(k)
        unknown = set(c) - ISSUE_KEYS
        if unknown:
            raise ValueError(f"{k}: unknown key(s) {sorted(unknown)}")
        if "cycle" in c and c["cycle"] is not None and not _is_int(c["cycle"]):
            raise ValueError(f"{k}: cycle must be a cycle number or null")
        if "priority" in c and not (_is_int(c["priority"]) and 0 <= c["priority"] <= 4):
            raise ValueError(f"{k}: priority must be 0-4")
        if "estimate" in c and not (_is_int(c["estimate"]) and c["estimate"] >= 0):
            raise ValueError(f"{k}: estimate must be a non-negative integer")
        if "cancel" in c and not isinstance(c["cancel"], bool):
            raise ValueError(f"{k}: cancel must be true or false")
        if c.get("cancel") and c.get("estimate", 0) != 0:
            raise ValueError(f"{k}: a canceled issue's estimate is 0, not {c['estimate']}")
    stamp = changes.get("cycle")
    if stamp is not None:
        if not isinstance(stamp, dict):
            raise ValueError("`cycle` must be an object or null")
        unknown = set(stamp) - STAMP_KEYS
        if unknown:
            raise ValueError(f"cycle: unknown key(s) {sorted(unknown)}")
        if not _is_int(stamp.get("number")):
            raise ValueError("cycle.number must be a cycle number")
        if stamp.get("planning") not in (None, *cycles.PLANNING_STATUSES):
            raise ValueError(f"cycle.planning must be one of {', '.join(cycles.PLANNING_STATUSES)} or null")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class ActiveCycleMove(ValueError):
    """One or more cycle changes would move an issue out of a running cycle."""


def running(cycle: dict, now: datetime.datetime) -> bool:
    """Started and not yet closed by Linear. A cycle past endsAt whose close
    job has not run still counts: Linear is about to roll its work forward."""
    return cycles.parse_ts(cycle["startsAt"]) <= now and not cycle.get("completedAt")


def plan(issue_changes: list[dict], live: dict[str, dict], cycles_by_number: dict[int, dict],
         canceled_state_id: str | None, *, now: datetime.datetime,
         allow_active_move: bool = False) -> tuple[list, int]:
    """The idempotence step, pure. Returns (planned, skipped): planned is
    [(change, live issue, input)] where input holds only the IssueUpdateInput
    fields that differ from what is live. Raises LookupError for an unknown
    issue or cycle number, and ActiveCycleMove (listing every offender) when a
    cycle change would take an issue out of a running cycle — unless
    allow_active_move. Estimate, priority and cancel changes are never refused
    for that reason."""
    by_id = {c["id"]: c for c in cycles_by_number.values()}
    planned, skipped, refused = [], 0, []
    for c in issue_changes:
        k = c["identifier"]
        if k not in live:
            raise LookupError(f"{k} not found in Linear")
        cur = live[k]
        fields = {}
        if "cycle" in c:
            if c["cycle"] is None:
                want = None
            elif c["cycle"] in cycles_by_number:
                want = cycles_by_number[c["cycle"]]["id"]
            else:
                raise LookupError(f"{k}: cycle {c['cycle']} not found")
            cur_id = (cur.get("cycle") or {}).get("id")
            if cur_id != want:
                home = by_id.get(cur_id)
                if home is not None and running(home, now) and not allow_active_move:
                    ends = cycles.parse_ts(home["endsAt"]).astimezone(now.tzinfo).date()
                    state = "active" if now < cycles.parse_ts(home["endsAt"]) else "ended but unclosed"
                    refused.append(f"{k} is in {state} cycle {home['number']} (ends {ends}): Linear rolls "
                                   "unfinished work at close — apply this cycle change after it closes")
                else:
                    fields["cycleId"] = want
        if "priority" in c and cur["priority"] != c["priority"]:
            fields["priority"] = c["priority"]
        estimate = 0 if c.get("cancel") else c.get("estimate")
        if (c.get("cancel") or "estimate" in c) and cur["estimate"] != estimate:
            fields["estimate"] = estimate
        if c.get("cancel") and cur["state"]["type"] != "canceled":
            if canceled_state_id is None:
                raise LookupError("the team has no Canceled workflow state")
            fields["stateId"] = canceled_state_id
        if fields:
            planned.append((c, cur, fields))
        else:
            skipped += 1
    if refused:
        raise ActiveCycleMove("\n  ".join(refused))
    return planned, skipped


def plan_stamp(stamp: dict | None, cycles_by_number: dict[int, dict]) -> tuple[dict, str] | None:
    """(cycle, new description), or None when there is nothing to write.
    Raises ValueError past Linear's 255-char cap rather than truncate, and
    LookupError for an unknown cycle."""
    if not stamp or stamp.get("planning") is None:
        return None
    cycle = cycles_by_number.get(stamp["number"])
    if cycle is None:
        raise LookupError(f"cycle {stamp['number']} not found")
    after = cycles.set_planning(cycle.get("description"), stamp["planning"], stamp.get("note"))
    return None if after == (cycle.get("description") or "") else (cycle, after)


def describe(change: dict, cur: dict, fields: dict, cycles_by_id: dict[str, dict]) -> str:
    def cyc(cid):
        return f"cycle {cycles_by_id[cid]['number']}" if cid in cycles_by_id else "no cycle"

    parts = []
    if "cycleId" in fields:
        parts.append(f"cycle: {cyc((cur.get('cycle') or {}).get('id'))} -> {cyc(fields['cycleId'])}")
    if "priority" in fields:
        parts.append(f"priority: {cur['priority']} -> {fields['priority']}")
    if "estimate" in fields:
        parts.append(f"estimate: {cur['estimate']} -> {fields['estimate']}")
    if "stateId" in fields:
        parts.append(f"state: {cur['state']['name']} -> Canceled")
    return f"  {change['identifier']:<8} " + "; ".join(parts) + f"  — {cur['title'][:50]}"


def fetch_live(identifiers: list[str]) -> dict[str, dict]:
    if not identifiers:
        return {}
    nodes = gql(Q_LIVE, {"n": [int(k.split("-")[1]) for k in identifiers]})["issues"]["nodes"]
    warn_if_capped(nodes, 250, "issues to update")
    return {n["identifier"]: n for n in nodes}


def fetch_canceled_state() -> str | None:
    nodes = gql(Q_CANCELED)["workflowStates"]["nodes"]
    return nodes[0]["id"] if nodes else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--changes", required=True, help="changes JSON file")
    ap.add_argument("--confirm", action="store_true", help="required to write")
    ap.add_argument("--allow-active-cycle-move", action="store_true",
                    help="allow moving an issue out of a running cycle (only when the user explicitly asks)")
    args = ap.parse_args(argv)

    try:
        changes = json.loads(Path(args.changes).read_text())
        validate(changes)
        issue_changes = changes.get("issues", [])
        cycle_list = cycles.fetch_cycles()
        by_number = {c["number"]: c for c in cycle_list}
        live = fetch_live([c["identifier"] for c in issue_changes])
        canceled = fetch_canceled_state() if any(c.get("cancel") for c in issue_changes) else None
        planned, skipped = plan(issue_changes, live, by_number, canceled, now=datetime.datetime.now().astimezone(),
                                allow_active_move=args.allow_active_cycle_move)
        stamp = plan_stamp(changes.get("cycle"), by_number)
    except (ValueError, LookupError) as e:
        sys.exit(f"REFUSING:\n  {e}\nnothing written" if isinstance(e, ActiveCycleMove)
                 else f"REFUSING: {e} — nothing written")

    by_id = {c["id"]: c for c in cycle_list}
    for c, cur, fields in planned:
        print(describe(c, cur, fields, by_id))
        print(f"           issueUpdate {json.dumps(fields)}")
    if stamp:
        cycle, after = stamp
        print(f"  cycle {cycle['number']} description")
        print(f"    before: {cycle.get('description')!r}")
        print(f"    after:  {after!r}")
    print(f"\n{len(planned)} issue(s) to update, {skipped} already correct; "
          f"cycle stamp: {'to write' if stamp else 'nothing to write'}")

    if not args.confirm:
        print("\ndry run — nothing written; pass --confirm to write")
        return 0

    written = 0
    for c, cur, fields in planned:
        if not gql(M_ISSUE, {"id": cur["id"], "input": fields})["issueUpdate"]["success"]:
            sys.exit(f"issueUpdate did not succeed for {c['identifier']} after {written} update(s); "
                     "re-run to continue (already-applied fields are skipped)")
        written += 1
        print(f"  updated {c['identifier']}")
    if stamp:
        cycle, after = stamp
        if not cycles.mark_planning(cycle["id"], after).get("success"):
            sys.exit(f"cycleUpdate did not succeed for cycle {cycle['number']}")
        print(f"  stamped cycle {cycle['number']}")
    print(f"\n{written} issue(s) updated" + (", cycle stamped" if stamp else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
