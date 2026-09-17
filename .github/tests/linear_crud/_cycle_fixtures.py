"""Cycle and issue fixtures in the shapes live Linear returns (SB-626).

Timestamps, history samples and descriptions for cycles 2-4 are copied from
the SB team's real cycles; the issue lists are synthetic but preserve the
properties that matter: cycle 3 closed at 11/37 issues and 48/129 pts per its
history arrays, current membership has since drifted to 10 completed issues /
46 pts, and the 26 issues rolled forward at close now sum to 71 pts (history
says 129 - 48 = 81), 7 of them adhoc.

Not a test module (no test_ prefix), so discovery does not collect it.
"""

import copy
import datetime

UTC = datetime.timezone.utc

C3_DESCRIPTION = (
    "Planning: planned\n"
    "Shipped: 11/37 issues, 48/129 pts\n"
    "Carry-out: 26 issues / 81 pts (19 planned, 7 adhoc) -> cycle 4\n"
    "Adhoc share of completed: 45% issues, 48% pts\n"
    "Note: TRD firefighting took 5 of 11 completed issues."
)

C4_DESCRIPTION = (
    "Planning: skipped -- focus set ad hoc 2026-08-16\n"
    "Carry-in: 26 issues / 81 pts from cycle 3 (100% of opening scope)\n"
    "Focus: agentic delivery (507,506,437,624,625) + MTA match weekend (593-597)\n"
    "Intent: ~32 of 81 pts; rest parked, not committed."
)


def at(s):
    """An aware UTC datetime from a Linear-style '...Z' string."""
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _cycle(n, starts, ends, description=None, completed=None, **history):
    return {
        "id": f"cycle-{n}-uuid",
        "number": n,
        "startsAt": starts,
        "endsAt": ends,
        "completedAt": completed,
        "description": description,
        "issueCountHistory": history.get("issues", []),
        "completedIssueCountHistory": history.get("done_issues", []),
        "scopeHistory": history.get("scope", []),
        "completedScopeHistory": history.get("done_scope", []),
    }


def cycles():
    """Cycles 2, 3, 4 as fetch_cycles returns them. Fresh copies every call.

    2 and 3 are closed by Linear (completedAt ~30s after endsAt, as live); 4 is
    not, so it doubles as the "ended by the clock, not yet closed" case."""
    return copy.deepcopy([
        _cycle(
            2, "2026-08-02T04:00:00.000Z", "2026-08-09T04:00:00.000Z",
            completed="2026-08-09T04:00:42.670Z",
            issues=[20, 22, 24], done_issues=[0, 4, 9],
            scope=[60, 66, 70], done_scope=[0, 12, 30],
        ),
        _cycle(
            3, "2026-08-09T04:00:00.000Z", "2026-08-16T04:00:00.000Z", C3_DESCRIPTION,
            completed="2026-08-16T04:00:28.832Z",
            issues=[29, 29, 30, 31, 32, 33, 37, 37],
            done_issues=[0, 1, 2, 3, 5, 6, 11, 11],
            scope=[97, 97, 100, 105, 110, 115, 129, 129],
            done_scope=[0, 5, 8, 12, 20, 25, 48, 48],
        ),
        _cycle(4, "2026-08-16T04:00:00.000Z", "2026-08-23T04:00:00.000Z", C4_DESCRIPTION),
    ])


def by_number(cs, n):
    return next(c for c in cs if c["number"] == n)


def issue(n, estimate, *, state="completed", labels=(), created="2026-08-01T12:00:00.000Z", title=None):
    names = {"completed": "Done", "started": "In Progress", "unstarted": "Todo",
             "backlog": "Backlog", "canceled": "Canceled"}
    return {
        "identifier": f"SB-{n}",
        "title": title or f"issue {n}",
        "estimate": estimate,
        "createdAt": created,
        "state": {"name": names.get(state, state), "type": state},
        "labels": {"nodes": [{"name": lbl} for lbl in labels]},
    }


# Cycle 3's completed members as they read TODAY: 10 issues, 46 pts, one of
# which was created mid-cycle. History says 11/48 at close — the difference is
# drift, and the totals must not follow it.
_C3_DONE = [
    (101, 8, ("DOT", "driven:human")),
    (102, 5, ("DOT", "driven:agent-supervised")),
    (103, 5, ("TRD", "adhoc", "driven:human")),
    (104, 3, ("TRD", "adhoc", "driven:agent-auto")),
    (105, 8, ("MT", "driven:agent-auto")),
    (106, 3, ("MT", "adhoc")),
    (107, 5, ("DOT", "driven:human")),
    (108, 3, ("TRD", "adhoc", "driven:human")),
    (109, 2, ("DOT", "driven:agent-auto")),
    (110, 4, ("MT", "driven:human")),
]

# The 26 rolled forward at close, with CURRENT estimates: 19 planned summing
# 52, 7 adhoc summing 19 — 71 pts, 10 short of what history recorded.
_C3_CARRIED_PLANNED = [5, 3, 3, 2, 2, 3, 5, 1, 2, 3, 3, 2, 5, 1, 2, 3, 3, 2, 2]
_C3_CARRIED_ADHOC = [3, 2, 3, 5, 2, 3, 1]


def c3_members():
    done = [
        issue(n, est, labels=lbls,
              created="2026-08-12T15:00:00.000Z" if n == 103 else "2026-08-05T15:00:00.000Z")
        for n, est, lbls in _C3_DONE
    ]
    # A canceled member is neither done nor carried: it must not reach the
    # population of an ended cycle at all.
    return done + [issue(199, 3, state="canceled", labels=("DOT",))]


def c3_uncompleted():
    planned = [issue(200 + k, est, state="unstarted", labels=("DOT",))
               for k, est in enumerate(_C3_CARRIED_PLANNED)]
    adhoc = [issue(300 + k, est, state="started", labels=("TRD", "adhoc"))
             for k, est in enumerate(_C3_CARRIED_ADHOC)]
    return planned + adhoc


# ------------------------------------------------------------ SB-1087

def make_cycle(n, starts, ends, description=None, completed=None, **history):
    """A cycle beyond the 2-4 fixture set, same shape as cycles()."""
    return _cycle(n, starts, ends, description, completed, **history)


def c5():
    """Cycle 5, the week after cycle 4. Not closed."""
    return make_cycle(5, "2026-08-23T04:00:00.000Z", "2026-08-30T04:00:00.000Z")


def rich(base, *, priority=0, description=None, started=None, updated=None, project=None,
         blocks=(), blocked_by=(), cycle=None, history=()):
    """An issue() with the fields cycle_state's member and candidate queries
    add. `blocks` / `blocked_by` are (identifier, state type) pairs; `history`
    is (createdAt, [added label names]) newest first, as Linear returns it."""
    out = dict(base)
    out.update({
        "url": f"https://linear.app/silverbeer/issue/{base['identifier']}",
        "priority": priority,
        "description": description,
        "startedAt": started,
        "updatedAt": updated or base["createdAt"],
        "project": {"name": project} if project else None,
        "relations": {"nodes": [
            {"type": "blocks", "relatedIssue": {"identifier": k, "state": {"type": t}}} for k, t in blocks]},
        "inverseRelations": {"nodes": [
            {"type": "blocks", "issue": {"identifier": k, "state": {"type": t}}} for k, t in blocked_by]},
        "cycle": {"id": cycle["id"], "number": cycle["number"]} if cycle else None,
        "history": {"nodes": [
            {"createdAt": ts, "addedLabels": [{"name": n} for n in added]} for ts, added in history]},
    })
    return out
