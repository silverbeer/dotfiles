#!/usr/bin/env python3
"""cycle_state.py — one cycle's state as ONE JSON document (SB-1087).

Read-only. The numbers the PO agent quotes come from here and nowhere else:
`/cycle review` and `/cycle plan` read this JSON and never do arithmetic in
prose, and the standup (SB-1088), runner feed (SB-1090) and Telegram chat
(SB-1089) read the same document. The schema is documented field by field in
../SKILL.md; `schema` is bumped when a field is renamed or removed, not when
one is added.

    python3 cycle_state.py                        # the active cycle
    python3 cycle_state.py --cycle next           # next cycle, with a plan
    python3 cycle_state.py --cycle plan-target    # whichever cycle /cycle plan should plan
    python3 cycle_state.py --cycle 7
    python3 cycle_state.py --cycle current --plan --include SB-12 --exclude SB-40
    python3 cycle_state.py --cycle plan-target --pin SB-1088 --pin SB-989
    python3 cycle_state.py --as-of 2026-09-20T09:00:00-04:00

Pure functions below take and return plain dicts, so everything but main()
and Fetcher is testable offline. Errors go to stderr with a non-zero exit.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sys
from fractions import Fraction
from pathlib import Path

# linear-crud and cycle-runner are sibling skills. Resolve each from, in order:
# an env override (tests point these at a scratch copy), the sibling skill in
# the chezmoi source tree, then the deployed ~/.claude copy — the same dance as
# backlog-groom's apply.py and cycle-runner's pick.py.
_SKILLS = Path(__file__).resolve().parents[2]


def _resolve(env: str, skill: str, marker: str) -> Path:
    candidates = [_SKILLS / skill / "scripts", Path.home() / ".claude/skills" / skill / "scripts"]
    if os.environ.get(env):
        candidates.insert(0, Path(os.environ[env]))
    return next((p for p in candidates if (p / marker).is_file()), candidates[-1])


LINEAR_CRUD = _resolve("LINEAR_CRUD_SCRIPTS", "linear-crud", "cycles.py")
CYCLE_RUNNER = _resolve("CYCLE_RUNNER_SCRIPTS", "cycle-runner", "pick.py")
sys.path.insert(0, str(LINEAR_CRUD))
sys.path.insert(1, str(CYCLE_RUNNER))
try:
    import cycles  # noqa: E402
    from linear_api import gql  # noqa: E402  (tests monkeypatch cycle_state.gql)
except ImportError:
    sys.exit(f"cycle_state: missing {LINEAR_CRUD}/cycles.py — the linear-crud skill must be installed")
try:
    import pick  # noqa: E402  (board graph helpers, gate labels, priority order — imported, never forked)
except ImportError:
    sys.exit(f"cycle_state: missing {CYCLE_RUNNER}/pick.py — the cycle-runner skill must be installed")

SCHEMA = "po-agent.cycle_state/1"
TEAM = "SB"
CLOSED_TYPES = ("completed", "canceled")
NOT_STARTED_TYPES = ("triage", "backlog", "unstarted")
# /cycle plan targets the ACTIVE cycle instead of the next one while it is
# unstamped and at most this many days old: the boundary day and the weekend
# after it are when the plan actually gets made.
PLAN_GRACE_DAYS = 2

# A heading or bold lead-in naming the criteria, or a markdown checkbox. Prose
# that merely mentions "acceptance" does not count.
_AC_HEADING = re.compile(r"(?im)^\s*(#{1,6}\s*|\*\*)\s*(acceptance|done when|definition of done|exit criteria)")
_AC_CHECKBOX = re.compile(r"(?m)^\s*[-*]\s*\[[ xX]\]")

_RELATIONS = """relations{nodes{type relatedIssue{identifier state{type}}}}
           inverseRelations{nodes{type issue{identifier state{type}}}}"""

# On top of what cycles.summarize() needs.
MEMBER_EXTRA = f"url priority description startedAt updatedAt project{{name}} {_RELATIONS}"

_CANDIDATE_FIELDS = f"""identifier title url estimate priority createdAt description
           state{{name type}} labels{{nodes{{name}}}} cycle{{id number}} {_RELATIONS}"""

Q_GATED = """
query($labels:[String!]){ issues(filter:{team:{key:{eq:"SB"}}, labels:{name:{in:$labels}},
    state:{type:{nin:["completed","canceled"]}}}, first:100){
  nodes{ identifier title url updatedAt state{name type} cycle{id number}
         labels{nodes{name}} history(first:10){nodes{createdAt addedLabels{name}}} } } }
"""

Q_BACKLOG = """
{ issues(filter:{team:{key:{eq:"SB"}}, cycle:{null:true}, state:{type:{in:["backlog","unstarted"]}},
    priority:{gte:1, lte:2}}, first:100){ nodes{ %s } } }
""" % _CANDIDATE_FIELDS

Q_BY_NUMBER = """
query($n:[Float!]){ issues(filter:{team:{key:{eq:"SB"}}, number:{in:$n}}, first:100){ nodes{ %s } } }
""" % _CANDIDATE_FIELDS


# ------------------------------------------------------------------ time


def days_since(ts: str, now: datetime.datetime) -> int:
    """Whole days elapsed, floored: 71 hours is 2."""
    return int((now - cycles.parse_ts(ts)).total_seconds() // 86400)


def days_elapsed(cycle: dict, now: datetime.datetime) -> int | None:
    """Calendar days since an ACTIVE cycle started (0 on its first day), in
    now's timezone; None for a cycle that is not active."""
    if cycles.cycle_phase(cycle, now) != "active":
        return None
    return -cycles.days_until(cycle["startsAt"], now)


def next_cycle(cycle_list: list[dict], ref: dict) -> dict | None:
    """The earliest cycle starting at or after ref ends (endsAt is exclusive,
    so the next one normally starts at exactly that instant)."""
    end = cycles.parse_ts(ref["endsAt"])
    later = [c for c in cycle_list if cycles.parse_ts(c["startsAt"]) >= end]
    return min(later, key=lambda c: cycles.parse_ts(c["startsAt"])) if later else None


def resolve_cycle(cycle_list: list[dict], mode: str, now: datetime.datetime) -> tuple[dict, str | None]:
    """(cycle, plan reason). The reason is set only for the modes that plan by
    default, `next` and `plan-target`. Raises LookupError."""
    if mode == "current":
        return cycles.select_cycle(cycle_list, now=now), None
    if mode in ("next", "plan-target"):
        cur = cycles.select_cycle(cycle_list, now=now)
        if mode == "plan-target" and cycles.cycle_phase(cur, now) == "active":
            status = cycles.parse_stamp(cur.get("description"))["planning"]
            elapsed = days_elapsed(cur, now)
            if status is None and elapsed <= PLAN_GRACE_DAYS:
                return cur, (f"active cycle {cur['number']} has no planning stamp and is "
                             f"{elapsed} day(s) in (<= {PLAN_GRACE_DAYS})")
        nxt = next_cycle(cycle_list, cur)
        if nxt is None:
            raise LookupError(f"no cycle after cycle {cur['number']}")
        if mode == "next":
            return nxt, f"next cycle after cycle {cur['number']}"
        if cycles.cycle_phase(cur, now) != "active":
            why = f"cycle {cur['number']} is not active"
        elif cycles.parse_stamp(cur.get("description"))["planning"] is not None:
            why = f"active cycle {cur['number']} is already stamped"
        else:
            why = f"active cycle {cur['number']} is more than {PLAN_GRACE_DAYS} days in"
        return nxt, f"{why}; planning the next cycle"
    return cycles.select_cycle(cycle_list, number=int(mode), now=now), None


# --------------------------------------------------------------- issues


def has_ac(description: str | None) -> bool:
    d = description or ""
    return bool(_AC_HEADING.search(d) or _AC_CHECKBOX.search(d))


def is_open(issue: dict) -> bool:
    return issue["state"]["type"] not in CLOSED_TYPES


def repo_of(issue: dict) -> str | None:
    """The repo label, by the same repos.json table board.py renders with."""
    return next((n["name"] for n in issue["labels"]["nodes"] if n["name"] in pick.board.GH), None)


def open_blockers(issue: dict) -> list[str]:
    return [r["issue"]["identifier"] for r in (issue.get("inverseRelations") or {}).get("nodes", [])
            if r["type"] == "blocks" and r["issue"]["state"]["type"] not in CLOSED_TYPES]


def open_blocks(issue: dict) -> list[str]:
    return [r["relatedIssue"]["identifier"] for r in (issue.get("relations") or {}).get("nodes", [])
            if r["type"] == "blocks" and r["relatedIssue"]["state"]["type"] not in CLOSED_TYPES]


def last_activity(issue: dict) -> tuple[str | None, str | None]:
    """(timestamp, source): the later of updatedAt and startedAt.

    Issue history is deliberately not read. Linear's rollover at cycle close
    writes a history entry without bumping updatedAt, so history would make
    a ticket nobody has touched look freshly active."""
    seen = [(k, issue.get(k)) for k in ("updatedAt", "startedAt") if issue.get(k)]
    if not seen:
        return None, None
    key, ts = max(seen, key=lambda kv: cycles.parse_ts(kv[1]))
    return ts, key


def _number(identifier: str) -> int:
    return int(identifier.rsplit("-", 1)[1])


def issue_row(issue: dict, *, now: datetime.datetime, carry_in_ids: set[str]) -> dict:
    act, source = last_activity(issue)
    return {
        "identifier": issue["identifier"],
        "title": issue["title"],
        "url": issue.get("url"),
        "state": {"name": issue["state"]["name"], "type": issue["state"]["type"]},
        "estimate": issue["estimate"],
        "priority": issue.get("priority"),
        "adhoc": cycles.is_adhoc(issue),
        "driven": cycles.driven(issue),
        "gate": pick.pending_gate(issue),
        "project": (issue.get("project") or {}).get("name"),
        "repo": repo_of(issue),
        "created_at": issue["createdAt"],
        "started_at": issue.get("startedAt"),
        "last_activity": act,
        "activity_source": source,
        "days_since_activity": days_since(act, now) if act else None,
        "carry_in": issue["identifier"] in carry_in_ids,
        "has_ac": has_ac(issue.get("description")),
        "blocked_by": open_blockers(issue),
        "blocks": open_blocks(issue),
    }


def at_risk(rows: list[dict], *, days_left: int | None, at_risk_days: int, stale_days: int) -> dict:
    """Planned work not started with the cycle nearly out, and started work
    that has not moved. Adhoc tickets are never "not started" risk: nobody
    committed to them at planning."""
    late = days_left is not None and days_left <= at_risk_days
    not_started = [
        {k: r[k] for k in ("identifier", "title", "estimate", "priority")}
        for r in rows
        if late and not r["adhoc"] and r["state"]["type"] in NOT_STARTED_TYPES
    ]
    stalled = [
        {"identifier": r["identifier"], "title": r["title"], "state": r["state"]["name"],
         "days_since_activity": r["days_since_activity"]}
        for r in rows
        if r["state"]["type"] == "started"
        and r["days_since_activity"] is not None and r["days_since_activity"] >= stale_days
    ]
    stalled.sort(key=lambda r: -r["days_since_activity"])
    return {"not_started": not_started, "stalled": stalled}


def not_ready(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r["state"]["type"] in CLOSED_TYPES:
            continue
        missing = (["estimate"] if r["estimate"] is None else []) + ([] if r["has_ac"] else ["acceptance_criteria"])
        if missing:
            out.append({"identifier": r["identifier"], "title": r["title"], "missing": missing})
    return out


def waiting_on_human(gated: list[dict], *, cycle_id: str, now: datetime.datetime) -> list[dict]:
    """Workspace-wide, oldest first. Age runs from the newest history entry
    that added the CURRENT gate label; updatedAt when history does not reach
    back that far. Done/Canceled issues still carrying a gate label are not
    waiting on anyone."""
    out = []
    for i in gated:
        label = pick.pending_gate(i)
        if label is None or not is_open(i):
            continue
        added = [h["createdAt"] for h in (i.get("history") or {}).get("nodes", [])
                 if label in {lb["name"] for lb in (h.get("addedLabels") or [])}]
        if added:
            since, source = max(added, key=cycles.parse_ts), "history"
        else:
            since, source = i["updatedAt"], "updatedAt"
        out.append({
            "identifier": i["identifier"],
            "title": i["title"],
            "url": i.get("url"),
            "label": label,
            "since": since,
            "age_days": days_since(since, now),
            "age_source": source,
            "in_cycle": (i.get("cycle") or {}).get("id") == cycle_id,
        })
    out.sort(key=lambda r: cycles.parse_ts(r["since"]))
    return out


# -------------------------------------------------------------- velocity


def closed_before(cycle_list: list[dict], ref: dict, window: int) -> list[dict]:
    """The last `window` cycles Linear has CLOSED that ended by the time ref
    starts, oldest first. The clock alone is not enough: before the close job
    runs a cycle has no history totals and no carry-out."""
    start = cycles.parse_ts(ref["startsAt"])
    closed = [c for c in cycle_list if c.get("completedAt") and cycles.parse_ts(c["endsAt"]) <= start]
    closed.sort(key=lambda c: cycles.parse_ts(c["endsAt"]))
    return closed[-window:] if window > 0 else []


def velocity(window_cycles: list[dict], data: dict, *, window: int, now: datetime.datetime) -> tuple[dict, list]:
    """Mean completed points over the window, and capacity after the pooled
    adhoc share of DELIVERED points (not summarize()'s adhoc_share, which is
    of scope). `data` maps cycle id -> (members, uncompleted)."""
    warnings = []
    rows = []
    for c in window_cycles:
        members, uncompleted = data[c["id"]]
        s = cycles.summarize(c, members, uncompleted, now=now)
        rows.append({
            "number": c["number"],
            "points_done": s["totals"]["points"]["done"],
            "issues_done": s["totals"]["issues"]["done"],
            "planned_points_done": s["split"]["planned"]["points"]["done"],
            "adhoc_points_done": s["split"]["adhoc"]["points"]["done"],
        })
    if len(rows) < window:
        warnings.append(f"velocity uses {len(rows)} closed cycle(s), fewer than the window of {window}")
    if not rows:
        return {"cycles": [], "mean_points_done": None, "adhoc_share_points": None, "capacity_points": None,
                "formula": "no closed cycles: no velocity, no capacity"}, warnings
    done = sum(r["points_done"] for r in rows)
    adhoc = sum(r["adhoc_points_done"] for r in rows)
    split = sum(r["planned_points_done"] for r in rows) + adhoc
    mean = Fraction(done, len(rows))
    share = Fraction(adhoc, split) if split else Fraction(0)
    capacity = math.floor(mean * (1 - share))
    numbers = ", ".join(str(r["number"]) for r in rows)
    return {
        "cycles": rows,
        "mean_points_done": round(float(mean), 1),
        "adhoc_share_points": cycles.pct(adhoc, split),
        "capacity_points": capacity,
        "formula": (f"floor(mean points done {float(mean):.1f} over cycles {numbers}"
                    f" x (1 - adhoc {adhoc}/{split} pts done)) = {capacity}"),
    }, warnings


# ------------------------------------------------------------------ plan


def plan(target: dict, capacity: int | None, pools: list[tuple[str, list[dict]]], *,
         exclude: set[str] = frozenset(), pin: list[str] = (), reason: str | None = None,
         carry_over_projected: bool = False) -> tuple[dict, list]:
    """Rank candidates and fill to capacity. Never over it.

    `pools` is [(source, issues)] in precedence order: an issue in two pools
    keeps the first source. Order: pinned tickets first, in `pin` order; then
    carry-over, then priority (none last), then the critical path of the
    blocks graph among the candidates, then how many open tickets each
    unblocks, then oldest. Filling is greedy in that order: a ticket that
    would overshoot is listed in does_not_fit and the next is still tried —
    pinned or not. An unestimated ticket never fits. Raises ValueError when a
    ticket is both pinned and excluded.

    `carry_over_projected` says the carry-over pool is the still-running
    previous cycle's open work: ranked and fitted as if rolled, but not yet in
    the target, so no cycle move may be written for it before that cycle
    closes."""
    clash = sorted(set(pin) & set(exclude), key=_number)
    if clash:
        raise ValueError("pinned and excluded at once: " + ", ".join(clash))
    pins = list(dict.fromkeys(pin))
    warnings = []
    seen: dict[str, tuple[str, dict]] = {}
    for source, issues in pools:
        for i in issues:
            k = i["identifier"]
            if k in exclude or k in seen or not is_open(i):
                continue
            seen[k] = (source, i)

    graph = {k: [b for b in open_blocks(i) if b in seen] for k, (_, i) in seen.items()}
    graph = {k: v for k, v in graph.items() if v}
    loop = pick.board.find_cycle(graph)
    if loop:
        warnings.append("blocks graph among candidates has a cycle (" + " -> ".join(loop) + "); no critical path")
        chain = []
    else:
        chain = pick.board.longest_path(graph)
    critical = set(chain) if len(chain) >= 2 else set()

    ordered = sorted(seen.items(), key=lambda kv: (
        pins.index(kv[0]) if kv[0] in pins else len(pins),
        kv[1][0] != "carry_over",
        pick._priority_rank(kv[1][1].get("priority")),
        kv[0] not in critical,
        -len(open_blocks(kv[1][1])),
        kv[1][1]["createdAt"],
    ))

    candidates, fits, does_not_fit, needs_estimate = [], [], [], []
    cumulative = 0
    for rank, (k, (source, i)) in enumerate(ordered, 1):
        est = i["estimate"]
        if est is None:
            fit = False
            needs_estimate.append(k)
        elif capacity is not None and cumulative + est <= capacity:
            fit = True
            cumulative += est
            fits.append(k)
        else:
            fit = False
            does_not_fit.append(k)
        candidates.append({
            "identifier": k,
            "title": i["title"],
            "source": source,
            "pinned": k in pins,
            "projected": carry_over_projected and source == "carry_over",
            "rank": rank,
            "estimate": est,
            "priority": i.get("priority"),
            "repo": repo_of(i),
            "on_critical_path": k in critical,
            "unblocks": len(open_blocks(i)),
            "has_ac": has_ac(i.get("description")),
            "fits": fit,
            "cumulative_points": cumulative,
        })

    carry = cycles.points([i for _, (s, i) in ordered if s == "carry_over"])
    pinned = cycles.points([i for k, (_, i) in ordered if k in pins])
    return {
        "target": {"number": target["number"], "id": target["id"], "starts_at": target["startsAt"],
                   "reason": reason},
        "capacity_points": capacity,
        "proposed_points": cumulative,
        "candidates": candidates,
        "fits": fits,
        "does_not_fit": does_not_fit,
        "needs_estimate": needs_estimate,
        "carry_over_exceeds_capacity": capacity is not None and carry > capacity,
        "pinned_exceeds_capacity": capacity is not None and pinned > capacity,
        "carry_over_projected": carry_over_projected,
    }, warnings


# ----------------------------------------------------------------- fetch


class Fetcher:
    """The only code that talks to Linear. Caches per cycle, so a cycle that
    is both the predecessor and in the velocity window is fetched once."""

    def __init__(self):
        self._members: dict[tuple[str, str], list] = {}
        self._uncompleted: dict[str, list] = {}
        self.warnings: list[str] = []

    def _capped(self, nodes: list, cap: int, what: str) -> list:
        if len(nodes) >= cap:
            self.warnings.append(f"{what} hit the first:{cap} cap; results may be truncated")
        return nodes

    def cycles(self) -> list[dict]:
        return self._capped(cycles.fetch_cycles(), 50, "cycles")

    def members(self, cycle: dict, extra: str = "") -> list[dict]:
        key = (cycle["id"], extra)
        if key not in self._members:
            self._members[key] = self._capped(cycles.fetch_members(cycle["id"], extra),
                                              250, f"cycle {cycle['number']} issues")
        return self._members[key]

    def uncompleted(self, cycle: dict) -> list[dict]:
        if cycle["id"] not in self._uncompleted:
            self._uncompleted[cycle["id"]] = self._capped(cycles.fetch_uncompleted_upon_close(cycle["id"]),
                                                          250, f"cycle {cycle['number']} carry-out")
        return self._uncompleted[cycle["id"]]

    def gated(self) -> list[dict]:
        nodes = gql(Q_GATED, {"labels": list(pick.PENDING_GATE_LABELS)})["issues"]["nodes"]
        return self._capped(nodes, 100, "gated issues")

    def backlog(self) -> list[dict]:
        return self._capped(gql(Q_BACKLOG)["issues"]["nodes"], 100, "backlog candidates")

    def by_identifier(self, identifiers: list[str]) -> list[dict]:
        if not identifiers:
            return []
        nodes = gql(Q_BY_NUMBER, {"n": [_number(k) for k in identifiers]})["issues"]["nodes"]
        return self._capped(nodes, 100, "included issues")


# ----------------------------------------------------------------- build


def build(fetch: Fetcher, mode: str, *, now: datetime.datetime, at_risk_days: int = 3, stale_days: int = 3,
          velocity_window: int = 3, include: list[str] = (), exclude: list[str] = (), pin: list[str] = (),
          want_plan: bool = False) -> dict:
    clash = sorted(set(pin) & set(exclude), key=_number)
    if clash:
        raise ValueError("--pin and --exclude name the same ticket: " + ", ".join(clash))
    cycle_list = fetch.cycles()
    cycle, plan_reason = resolve_cycle(cycle_list, mode, now)
    warnings: list[str] = []

    closed = bool(cycle.get("completedAt"))
    members = fetch.members(cycle, MEMBER_EXTRA)
    uncompleted = fetch.uncompleted(cycle) if closed else None
    prev = None
    if not closed and cycles.cycle_phase(cycle, now) != "future":
        prev = cycles.previous_cycle(cycle_list, cycle)
    prev_uncompleted = fetch.uncompleted(prev) if prev and prev.get("completedAt") else None
    summary = cycles.summarize(cycle, members, uncompleted, prev_uncompleted, prev_cycle=prev, now=now)

    carry_in_ids = set((summary["carry_in"] or {}).get("identifiers", []))
    rows = sorted((issue_row(i, now=now, carry_in_ids=carry_in_ids) for i in members),
                  key=lambda r: _number(r["identifier"]))

    window = closed_before(cycle_list, cycle, velocity_window)
    vel, vel_warnings = velocity(
        window, {c["id"]: (fetch.members(c), fetch.uncompleted(c)) for c in window},
        window=velocity_window, now=now,
    )
    warnings += vel_warnings

    plan_doc = None
    if want_plan or plan_reason is not None:
        before = cycles.previous_cycle(cycle_list, cycle)
        open_members = [i for i in members if is_open(i)]
        projected = before is not None and not before.get("completedAt")
        if projected:
            # Not closed yet: its unfinished work will roll into this cycle at
            # close, so it is a projection — still in `before` today.
            carry_over = [i for i in fetch.members(before, MEMBER_EXTRA) if is_open(i)]
            in_target = open_members
        else:
            carry_over = [i for i in open_members if i["identifier"] in carry_in_ids]
            in_target = [i for i in open_members if i["identifier"] not in carry_in_ids]
        known = {i["identifier"] for i in carry_over + in_target}
        backlog = fetch.backlog()
        known |= {i["identifier"] for i in backlog}
        # Pins come from anywhere, through the same single fetch as --include.
        wanted = list(dict.fromkeys(k for k in [*include, *pin] if k not in known))
        included = fetch.by_identifier(wanted)
        found = {i["identifier"] for i in included}
        for flag, keys in (("--include", include), ("--pin", pin)):
            missing = sorted({k for k in keys if k in wanted} - found, key=_number)
            if missing:
                warnings.append(f"{flag} not found in Linear: " + ", ".join(missing))
            for i in sorted(included, key=lambda i: _number(i["identifier"])):
                if i["identifier"] in keys and not is_open(i):
                    warnings.append(f"{flag} {i['identifier']} is {i['state']['name']} ({i['state']['type']});"
                                    " not a candidate")
        plan_doc, plan_warnings = plan(
            cycle, vel["capacity_points"],
            [("carry_over", carry_over), ("in_target", in_target), ("backlog", backlog), ("included", included)],
            exclude=set(exclude), pin=pin, reason=plan_reason or "requested with --plan",
            carry_over_projected=projected,
        )
        warnings += plan_warnings

    warnings = fetch.warnings + warnings
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "team": TEAM,
        "mode": mode,
        "params": {"at_risk_days": at_risk_days, "stale_days": stale_days, "velocity_window": velocity_window,
                   "include": list(include), "exclude": list(exclude), "pin": list(pin)},
        "cycle": {
            **summary["cycle"],
            "days_elapsed": days_elapsed(cycle, now),
            "planning": summary["planning"],
        },
        "summary": summary,
        "issues": rows,
        "at_risk": at_risk(rows, days_left=summary["cycle"]["days_left"], at_risk_days=at_risk_days,
                           stale_days=stale_days),
        "waiting_on_human": waiting_on_human(fetch.gated(), cycle_id=cycle["id"], now=now),
        "not_ready": not_ready(rows),
        "velocity": vel,
        "plan": plan_doc,
        "warnings": warnings,
    }


# ------------------------------------------------------------------ CLI


def _mode(s: str) -> str:
    if s in ("current", "next", "plan-target") or s.isdigit():
        return s
    raise argparse.ArgumentTypeError(f"--cycle must be current, next, plan-target or a number, not {s!r}")


def _identifier(s: str) -> str:
    k = s.strip().upper()
    if not re.fullmatch(rf"{TEAM}-\d+", k):
        raise argparse.ArgumentTypeError(f"not an issue key like {TEAM}-123: {s!r}")
    return k


def _as_of(s: str) -> datetime.datetime:
    """A bare date is local midnight; a naive datetime is local time."""
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date or datetime: {s!r}") from None
    return dt if dt.tzinfo else dt.astimezone()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cycle", type=_mode, default="current", metavar="current|next|plan-target|N")
    ap.add_argument("--plan", action="store_true", help="fill `plan` for the selected cycle")
    ap.add_argument("--include", type=_identifier, action="append", default=[], metavar="SB-N",
                    help="force a ticket into plan consideration (repeatable)")
    ap.add_argument("--exclude", type=_identifier, action="append", default=[], metavar="SB-N",
                    help="remove a ticket from plan consideration (repeatable)")
    ap.add_argument("--pin", type=_identifier, action="append", default=[], metavar="SB-N",
                    help="rank a ticket first in the plan, in the order given; still bound by capacity (repeatable)")
    ap.add_argument("--at-risk-days", type=int, default=3)
    ap.add_argument("--stale-days", type=int, default=3)
    ap.add_argument("--velocity-window", type=int, default=3)
    ap.add_argument("--as-of", type=_as_of, metavar="ISO", help="evaluate as if now were this (naive = local)")
    args = ap.parse_args(argv)

    now = args.as_of or datetime.datetime.now().astimezone()
    try:
        doc = build(Fetcher(), args.cycle, now=now, at_risk_days=args.at_risk_days, stale_days=args.stale_days,
                    velocity_window=args.velocity_window, include=args.include, exclude=args.exclude,
                    pin=args.pin, want_plan=args.plan)
    except (LookupError, ValueError) as e:
        sys.exit(f"cycle_state: {e}")
    print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
