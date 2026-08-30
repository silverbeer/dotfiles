#!/usr/bin/env python3
"""pick.py — choose one ready ticket for cycle-runner to start (SB-929).

Queries Linear for open (`unstarted`/`started`) SB issues carrying a `driven:*`
label, filters to the ready queue (no open blocker — same definition as
`board.py`'s: a `blocks` relation whose related issue is not Done/Canceled),
applies the autonomy policy below, and prints ONE ticket key on stdout — or
nothing, exit 0, if none is eligible. `run.sh` is the only caller.

Policy (in order):
  - no `driven:*` label at all, or `driven:human` -> never picked (the query
    itself only asks for driven:* issues, so "no label" never reaches here;
    `driven:human` is filtered out explicitly, defensively)
  - `driven:agent-auto` -> eligible only if estimate <= 2 AND the issue carries
    `adhoc` or `chore`. `agent-auto` failing that combination is a POLICY
    VIOLATION (someone hand-set the label past what it means) — skipped, with
    a warning naming the ticket, never silently promoted to supervised or
    silently dropped without saying why.
  - `driven:agent-supervised` -> eligible regardless of estimate/label.

Among eligible ready tickets: sort by priority (Linear's own 1=Urgent..4=Low,
0=No priority sorts last — same convention `linear.sh list`'s "P$priority"
column exposes) then by `createdAt` (oldest first), and print the winner.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# linear_api.py lives in the linear-crud skill. Resolve it from, in order:
# LINEAR_CRUD_SCRIPTS (tests point this at a scratch copy), the sibling skill
# in the chezmoi source tree, then the deployed ~/.claude copy — same dance as
# backlog-groom's apply.py/fetch.py.
_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "linear-crud" / "scripts",
    Path.home() / ".claude/skills/linear-crud/scripts",
]
if os.environ.get("LINEAR_CRUD_SCRIPTS"):
    _CANDIDATES.insert(0, Path(os.environ["LINEAR_CRUD_SCRIPTS"]))
LINEAR_CRUD = next((p for p in _CANDIDATES if (p / "linear_api.py").is_file()), _CANDIDATES[-1])
sys.path.insert(0, str(LINEAR_CRUD))
try:
    from linear_api import gql, warn_if_capped  # noqa: E402  (tests monkeypatch pick.gql)
except ImportError:
    sys.exit(f"pick: missing {LINEAR_CRUD}/linear_api.py — the linear-crud skill must be installed")


def _load_board():
    """board.py carries chezmoi's `executable_` prefix in the source tree and
    is undecorated once deployed (`sibling()` in linear.sh resolves the same
    ambiguity for shell). A python `import board` can't follow two possible
    module file names, so resolve the path and `exec` it under a fixed module
    name instead — the same trick `.github/tests/linear_crud/_load.py` uses
    for the equivalent problem in tests, here at runtime."""
    for name in ("board.py", "executable_board.py"):
        p = LINEAR_CRUD / name
        if p.is_file():
            spec = importlib.util.spec_from_file_location("cycle_runner_board", p)
            mod = importlib.util.module_from_spec(spec)
            if spec.loader is None:  # pragma: no cover — importlib always sets one for a file location
                sys.exit(f"pick: could not load {p}")
            sys.modules["cycle_runner_board"] = mod
            spec.loader.exec_module(mod)
            return mod
    sys.exit(f"pick: board.py not found next to linear.sh in {LINEAR_CRUD} — run doctor.sh")


board = _load_board()

# board.Q_ISSUES already carries the exact graph shape pick.py needs (labels,
# state, relations/inverseRelations for blocker detection) — reused verbatim
# rather than duplicated, with `priority` and `createdAt` spliced in for the
# tie-break sort board.py itself never needed (it groups by epic, not by
# pick order). A brittle-looking string edit, but the alternative is a second
# copy of this query that silently drifts from board.py's the day the shape
# changes there.
Q_READY = board.Q_ISSUES.replace(
    "nodes { identifier title estimate url",
    "nodes { identifier title estimate url priority createdAt",
)

DRIVEN_LABELS = ("driven:human", "driven:agent-supervised", "driven:agent-auto")
READY_STATE_TYPES = ("unstarted", "started")
# A blocking relation's target counts as resolved (no longer blocking) once it
# reaches either terminal state — board.py's own inverseRelations filter only
# checks `!= "Done"`, which is a live gap there (a Canceled blocker still
# reads as open on the board); pick.py does not inherit it.
CLOSED_STATES = ("Done", "Canceled")
# A gate in one of these states is waiting on a HUMAN, so the ticket is not
# workable no matter how ready the rest of it looks (SB-944). Draining a gate
# is step 2 of run.sh (handle_merge_gate); picking is step 3. Without this,
# step 3 hands a gate-blocked ticket to /work-headless every tick, pays ~180k
# tokens to rediscover the gate, gets `blocked` back, changes nothing — and
# does it again 30 minutes later. It also starves the queue: ready_queue()
# sorts by priority and run.sh takes [0], so one gated ticket at the front
# blocks everything behind it for as long as the human takes to answer.
#
# `gate:approved` / `gate:rejected` are deliberately NOT here: those are
# resolved, step 2 acts on them, and the ticket is workable again.
PENDING_GATE_LABELS = ("gate:awaiting-approval", "gate:needs-human")


def driven_label(issue: dict) -> str | None:
    """`driven:human` is a kill-switch: if an issue somehow carries it
    alongside an `agent-*` label (hand-added, list order not guaranteed),
    it must win regardless of position — ambiguity resolves toward NOT
    running, never toward running."""
    names = [n["name"] for n in issue["labels"]["nodes"] if n["name"].startswith("driven:")]
    if "driven:human" in names:
        return "driven:human"
    return names[0] if names else None


def is_ready(issue: dict) -> bool:
    """No open blocker: every inverse `blocks` relation points at an issue
    that has already reached Done or Canceled."""
    return not any(
        r["type"] == "blocks" and r["issue"]["state"]["name"] not in CLOSED_STATES
        for r in issue["inverseRelations"]["nodes"]
    )


def pending_gate(issue: dict) -> str | None:
    """The pending `gate:*` label on this issue, if any. `gate:*` is a
    mutually-exclusive Linear label group (see gatekeeper/scripts/gate.py),
    so at most one can be set — but this does not rely on that: any pending
    one is enough to withhold the ticket."""
    for name in (n["name"] for n in issue["labels"]["nodes"]):
        if name in PENDING_GATE_LABELS:
            return name
    return None


def policy_ok(issue: dict, driven: str) -> tuple[bool, str | None]:
    """(eligible, warning). A warning is only ever set on a POLICY VIOLATION —
    an issue whose driven:* label promises more autonomy than its
    estimate/label combination is allowed to carry."""
    if driven == "driven:agent-supervised":
        return True, None
    if driven == "driven:agent-auto":
        labels = {n["name"] for n in issue["labels"]["nodes"]}
        estimate = issue["estimate"]
        if estimate is not None and estimate <= 2 and ("adhoc" in labels or "chore" in labels):
            return True, None
        return False, (
            f"{issue['identifier']} is driven:agent-auto but fails policy "
            f"(estimate={estimate!r}, needs <=2 and adhoc|chore in labels={sorted(labels)!r}) "
            "— POLICY VIOLATION, skipping rather than silently promoting/demoting"
        )
    return False, None  # driven:human, or anything else — never picked, never warned


def _priority_rank(priority: int | None) -> int:
    # Linear: 0 = No priority, 1 Urgent .. 4 Low. Sort urgent-first; an unset
    # priority goes to the back rather than winning ties against a real one.
    return priority if priority else 5


def ready_queue(issues: list[dict]) -> list[dict]:
    out = []
    for issue in issues:
        driven = driven_label(issue)
        if driven is None or driven == "driven:human":
            continue
        if issue["state"]["type"] not in READY_STATE_TYPES:
            continue
        gate = pending_gate(issue)
        if gate is not None:
            # Not a policy violation — a normal, expected state. Logged so a
            # quiet tick is legible in the run log rather than looking like an
            # empty queue.
            print(f"note: pick: skipping {issue['identifier']} ({gate} — waiting on a human)", file=sys.stderr)
            continue
        if not is_ready(issue):
            continue
        eligible, warning = policy_ok(issue, driven)
        if warning:
            print(f"warn: pick: {warning}", file=sys.stderr)
        if not eligible:
            continue
        out.append(issue)
    out.sort(key=lambda i: (_priority_rank(i["priority"]), i["createdAt"]))
    return out


def main() -> int:
    team = os.environ.get("LINEAR_TEAM", "SB")
    issues = gql(Q_READY, {"labels": list(DRIVEN_LABELS), "team": team})["issues"]["nodes"]
    warn_if_capped(issues, 250, "pick candidate issues")
    queue = ready_queue(issues)
    if queue:
        print(queue[0]["identifier"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
