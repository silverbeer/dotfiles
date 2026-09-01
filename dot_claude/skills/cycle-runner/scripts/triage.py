#!/usr/bin/env python3
"""triage.py — batch metadata triage for Linear issues needing prep (SB-624 / T9).

Mirrors backlog-groom's fetch/apply split: `propose` fetches every SB issue
that needs prep and drafts a full change set in one pass, writing a batched
`review.md` (human-readable) and `apply.json` (machine-readable, same shape
`apply()`'s idempotence step expects — same idiom as
`backlog-groom/scripts/apply.py`). `apply` re-reads Linear immediately before
writing and refuses/skips any field that has drifted since propose ran.

An issue needs triage if it is in the Triage workflow state, OR is missing a
*type* label (bug|feature|chore|docs|infra|security — these are flat label
names in this workspace, not `type:`-prefixed; confirmed against
`linear.sh cmd_new`'s `-l "$type"`), OR has no estimate, OR carries no
`driven:*` label at all.

`type` and `estimate` cannot be read off the issue when they're missing —
there is no human in a headless run to ask, and this script is not an LLM —
so this deliberately makes best-effort, clearly-labelled GUESSES rather than
leaving a gap `apply` can't act on:
  - type: keyword match against the title, falling back to `chore` (the
    least consequential category to be wrong about).
  - estimate: a fixed default (GUESS_ESTIMATE, currently 3) — deliberately
    ABOVE the driven:agent-auto threshold of <=2, so a wrong guess can never
    by itself unlock unsupervised autonomy. A ticket only gets proposed
    driven:agent-auto here when its estimate was *already real* (not one of
    this script's guesses) and <=2, with `adhoc`/`chore` already on the
    issue for real — never from a freshly-guessed type or estimate. Reuses
    `pick.driven_label`/`pick.policy_ok` for that eligibility check, per the
    instruction in pick.py's own docstring not to fork this logic.

Both guesses are surfaced in review.md exactly as guesses, for the gate's
human to catch before approving.

    python3 triage.py propose --out-review review.md --out-apply apply.json
    python3 triage.py apply --changes apply.json            # dry run
    python3 triage.py apply --changes apply.json --confirm  # writes
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# linear_api.py lives in the linear-crud skill. Resolve it from, in order:
# LINEAR_CRUD_SCRIPTS (tests point this at a scratch copy), the sibling skill
# in the chezmoi source tree, then the deployed ~/.claude copy — same dance as
# backlog-groom's apply.py/fetch.py and pick.py.
_CANDIDATES = [
    SCRIPTS.parent.parent / "linear-crud" / "scripts",
    Path.home() / ".claude/skills/linear-crud/scripts",
]
if os.environ.get("LINEAR_CRUD_SCRIPTS"):
    _CANDIDATES.insert(0, Path(os.environ["LINEAR_CRUD_SCRIPTS"]))
LINEAR_CRUD = next((p for p in _CANDIDATES if (p / "linear_api.py").is_file()), _CANDIDATES[-1])
sys.path.insert(0, str(LINEAR_CRUD))
try:
    from linear_api import gql, warn_if_capped  # noqa: E402  (tests monkeypatch triage.gql)
except ImportError:
    sys.exit(f"triage: missing {LINEAR_CRUD}/linear_api.py — the linear-crud skill must be installed")

# pick.py is a plain sibling in this same scripts dir (no chezmoi executable_
# prefix ambiguity to resolve, unlike board.py) — driven_label/policy_ok are
# imported, never forked, per pick.py's own docstring warning.
sys.path.insert(0, str(SCRIPTS))
import pick  # noqa: E402

TYPE_GROUP = ("bug", "feature", "chore", "docs", "infra", "security")

# Checked in this order; first keyword match wins. `security` and `bug` sort
# before the generic `feature`/`chore` so an urgent-sounding title doesn't get
# swallowed by a looser match.
_TYPE_KEYWORDS = {
    "security": ("security", "cve", "vulnerab", "secret", "credential"),
    "bug": ("bug", "fix", "broken", "crash", "regression", "error"),
    "docs": ("doc", "readme", "documentation"),
    "infra": ("ci", "pipeline", "deploy", "launchd", "infra", "workflow", "docker", "plist"),
    "feature": ("add", "new", "feature", "support", "implement"),
}
GUESS_ESTIMATE = 3  # Fibonacci, deliberately > 2 — see module docstring.

Q_TRIAGE = """
{ issues(filter:{team:{key:{eq:"%s"}}, state:{type:{nin:["completed","canceled"]}}},
         first:250){
    nodes{ identifier title url estimate createdAt state{name type} labels{nodes{name}} }
} }
"""


def guess_type(title: str) -> str:
    lowered = title.lower()
    for type_name, keywords in _TYPE_KEYWORDS.items():
        if any(k in lowered for k in keywords):
            return type_name
    return "chore"


def needs_triage(issue: dict) -> bool:
    if issue["state"]["type"] == "triage":
        return True
    labels = {n["name"] for n in issue["labels"]["nodes"]}
    if not (labels & set(TYPE_GROUP)):
        return True
    if issue["estimate"] is None:
        return True
    if pick.driven_label(issue) is None:
        return True
    return False


def choose_driven(estimate: int | None, label_names: set[str]) -> str:
    """`driven:agent-auto` vs `driven:agent-supervised`, via pick.policy_ok —
    never re-derived here. `label_names` must be the issue's REAL existing
    labels, never including a type this run itself just guessed (see module
    docstring: a guess must never be allowed to unlock more autonomy)."""
    fake_issue = {
        "identifier": "?",
        "estimate": estimate,
        "labels": {"nodes": [{"name": n} for n in label_names]},
    }
    eligible, _warning = pick.policy_ok(fake_issue, "driven:agent-auto")
    return "agent-auto" if eligible else "agent-supervised"


def draft_changes(issue: dict) -> dict:
    """The proposal for one issue: only the fields it is actually missing.
    Pure — no network — so `propose()` and tests can both drive it directly."""
    labels = {n["name"] for n in issue["labels"]["nodes"]}
    change: dict = {"id": issue["identifier"], "title": issue["title"]}

    type_guessed = not (labels & set(TYPE_GROUP))
    if type_guessed:
        change["type"] = guess_type(issue["title"])

    estimate_guessed = issue["estimate"] is None
    if estimate_guessed:
        change["estimate"] = GUESS_ESTIMATE

    if pick.driven_label(issue) is None:
        effective_estimate = issue["estimate"] if not estimate_guessed else GUESS_ESTIMATE
        # Real labels only — see choose_driven()'s docstring.
        change["driven"] = choose_driven(effective_estimate, labels)

    return change


def fetch(team: str) -> list[dict]:
    nodes = gql(Q_TRIAGE % team)["issues"]["nodes"]
    warn_if_capped(nodes, 250, "open issues")
    return nodes


# ------------------------------------------------------------------ propose


def render_review(today: str, drafts: list[dict], issues_by_id: dict[str, dict]) -> str:
    changed = [d for d in drafts if len(d) > 2]  # more than just id/title
    untouched = [d for d in drafts if len(d) <= 2]

    out = [
        f"# Triage — review ({today})",
        "",
        f"{len(drafts)} issue(s) need triage. **Nothing has been written to Linear.**",
        "",
        "This is one batched proposal — approve or reject the whole thing via the",
        "gate. `type` and `estimate` are best-effort guesses wherever Linear had no",
        "signal to size or classify from — check those before approving; a wrong",
        "guess never grants driven:agent-auto by itself (see triage.py's docstring).",
        "",
    ]

    out += [f"## Proposed changes ({len(changed)})", ""]
    for d in changed:
        issue = issues_by_id[d["id"]]
        label_names = sorted(n["name"] for n in issue["labels"]["nodes"])
        out.append(f"### {d['id']} — {d['title']}")
        out.append(f"- trigger: state={issue['state']['name']} · labels: {', '.join(label_names) or '—'}")
        if "type" in d:
            out.append(f"- type: **{d['type']}** (guessed from title keywords)")
        if "estimate" in d:
            out.append(f"- estimate: **{d['estimate']}** (default guess — no signal to size from)")
        if "driven" in d:
            out.append(f"- driven: **{d['driven']}** (policy: agent-auto needs estimate<=2 and adhoc|chore)")
        out.append("")

    if untouched:
        out += [
            f"## Already fully triaged, still in Triage state ({len(untouched)})",
            "",
            "type/estimate/driven are all already set. Moving the workflow state is",
            "out of scope for this command — move these manually.",
            "",
        ]
        for d in untouched:
            issue = issues_by_id[d["id"]]
            out.append(f"- {d['id']} — {d['title']}")
        out.append("")

    return "\n".join(out)


def cmd_propose(args: argparse.Namespace) -> int:
    today = args.today or datetime.date.today().isoformat()
    issues = [i for i in fetch(args.team) if needs_triage(i)]
    issues.sort(key=lambda i: i["identifier"])  # stable, idempotent ordering
    drafts = [draft_changes(i) for i in issues]
    issues_by_id = {i["identifier"]: i for i in issues}

    Path(args.out_review).write_text(render_review(today, drafts, issues_by_id))
    Path(args.out_apply).write_text(
        json.dumps({"generated_for": today, "team": args.team, "changes": drafts}, indent=2)
    )

    changed = sum(1 for d in drafts if len(d) > 2)
    print(f"review → {args.out_review}", file=sys.stderr)
    print(
        f"apply  → {args.out_apply}  ({len(drafts)} issue(s) need triage, {changed} with proposed changes)",
        file=sys.stderr,
    )
    return 0


# -------------------------------------------------------------------- apply


def current_state(ids: list[str], team: str) -> dict[str, dict]:
    """`team` must be the SAME team `propose` fetched from (read back from
    apply.json's "team" field by the caller) — filtering by a different team
    here would make every id look "not found in Linear" and silently apply
    nothing, however correct the ids themselves are."""
    numbers = ",".join(i.split("-")[1] for i in ids)
    data = gql(
        '{ issues(filter:{number:{in:[%s]},team:{key:{eq:"%s"}}}, first:250){ nodes{'
        " id identifier estimate labels{nodes{id name}} } } }" % (numbers, team)
    )
    nodes = data["issues"]["nodes"]
    warn_if_capped(nodes, 250, "issues to triage-apply")
    return {n["identifier"]: n for n in nodes}


def label_ids() -> dict[str, str]:
    nodes = gql("{ issueLabels(first: 250) { nodes { id name } } }")["issueLabels"]["nodes"]
    warn_if_capped(nodes, 250, "issue labels")
    return {n["name"]: n["id"] for n in nodes}


def plan(changes: list[dict], live: dict[str, dict]) -> tuple[list, int, list[str]]:
    """The idempotence + drift-refusal step, kept pure so it's testable
    without Linear. Returns (planned, skipped, drift_notes):
      - planned: [(change, current, applicable_fields)] — only fields that
        are STILL missing on re-read make it into applicable_fields; a field
        someone else already set since propose ran is dropped, noted, never
        overwritten (this command only ever fills gaps).
      - skipped: count of changes with nothing left to apply (already fully
        triaged, or drifted on every proposed field).
      - drift_notes: human-readable notes for anything skipped or partially
        dropped, so `apply`'s dry-run output says why."""
    planned, skipped, drift_notes = [], 0, []
    for c in changes:
        cur = live.get(c["id"])
        if cur is None:
            drift_notes.append(f"{c['id']}: not found in Linear — skipping")
            skipped += 1
            continue
        cur_labels = {n["name"] for n in cur["labels"]["nodes"]}
        applicable: dict = {}
        local_notes = []

        if "type" in c:
            if cur_labels & set(TYPE_GROUP):
                local_notes.append(f"type already set ({sorted(cur_labels & set(TYPE_GROUP))}) — not overwriting")
            else:
                applicable["type"] = c["type"]

        if "estimate" in c:
            if cur["estimate"] is not None:
                local_notes.append(f"estimate already set ({cur['estimate']}) — not overwriting")
            else:
                applicable["estimate"] = c["estimate"]

        if "driven" in c:
            if any(n.startswith("driven:") for n in cur_labels):
                local_notes.append("driven already set — not overwriting")
            else:
                applicable["driven"] = c["driven"]

        if not applicable:
            skipped += 1
            if local_notes:
                drift_notes.append(f"{c['id']}: fully drifted since propose — {'; '.join(local_notes)}")
            continue
        if local_notes:
            drift_notes.append(f"{c['id']}: partially drifted — {'; '.join(local_notes)}")
        planned.append((c, cur, applicable))
    return planned, skipped, drift_notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("propose", help="fetch + draft the batched triage proposal")
    p.add_argument("--team", default="SB")
    p.add_argument("--out-review", required=True)
    p.add_argument("--out-apply", required=True)
    p.add_argument("--today", help="YYYY-MM-DD, for reproducible output (default: system date)")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("apply", help="re-read Linear and write only what still applies")
    p.add_argument("--changes", required=True)
    p.add_argument("--confirm", action="store_true", help="required to write")
    p.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    return args.func(args)


def cmd_apply(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.changes).read_text())
    # Recorded by propose (not re-guessed here) — see current_state()'s
    # docstring. .get() with a fallback to "SB" is only for apply.json files
    # written before this field existed; propose always writes it now.
    team = payload.get("team", "SB")
    changes = payload["changes"]
    changes = [c for c in changes if len(c) > 2]  # id/title-only rows had nothing proposed
    if not changes:
        print("nothing to apply")
        return 0

    live = current_state([c["id"] for c in changes], team)
    labels = label_ids()

    planned, skipped, drift_notes = plan(changes, live)

    for note in drift_notes:
        print(f"note: {note}", file=sys.stderr)

    unknown_labels = sorted(
        {
            v
            for c, _, applicable in planned
            for k, v in applicable.items()
            if k in ("type", "driven") and (v if k == "type" else f"driven:{v}") not in labels
        }
    )
    if unknown_labels:
        sys.exit("REFUSING: these labels do not exist in Linear — create them first:\n  " + "\n  ".join(unknown_labels))

    for c, cur, applicable in planned:
        print(f"  {c['id']:<8} {applicable}  — {c['title'][:56]}")
    print(f"\n{len(planned)} to update, {skipped} skipped (already correct or drifted)")

    if not args.confirm:
        print("\ndry run — pass --confirm to write")
        return 0

    written = 0
    failed = []
    for c, cur, applicable in planned:
        keep_ids = [n["id"] for n in cur["labels"]["nodes"]]
        add_ids = []
        if "type" in applicable:
            add_ids.append(labels[applicable["type"]])
        if "driven" in applicable:
            add_ids.append(labels[f"driven:{applicable['driven']}"])

        fields = {}
        if add_ids:
            fields["labelIds"] = keep_ids + add_ids
        if "estimate" in applicable:
            fields["estimate"] = applicable["estimate"]

        parts = [f"{k}: {json.dumps(v)}" for k, v in fields.items()]
        res = gql('mutation { issueUpdate(id: "%s", input: { %s }) { success } }' % (cur["id"], ", ".join(parts)))
        # Same success check as gate.py's set_gate_label / set-driven.py's
        # cmd_set — but a batched run reports per-issue and keeps going,
        # matching this file's own partial-failure idiom (drift_notes above),
        # rather than aborting the whole run on one stale label id.
        if not res["issueUpdate"]["success"]:
            failed.append(c["id"])
            print(f"  FAILED   {c['id']:<8} — issueUpdate returned success=false", file=sys.stderr)
            continue
        written += 1
        print(f"  updated {c['id']}")
    print(f"\n{written} issues updated" + (f", {len(failed)} FAILED to write: {', '.join(failed)}" if failed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
