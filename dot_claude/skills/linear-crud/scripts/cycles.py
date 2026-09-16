"""Cycle queries and arithmetic that survive a cycle closing (SB-626).

Everything cycle-report.py prints comes from here, as plain dicts, with no
printing and no exits, so other skills can import it (SB-1087). Import with the
scripts dir on sys.path:

    sys.path.insert(0, str(Path.home() / ".claude/skills/linear-crud/scripts"))
    from cycles import fetch_cycles, select_cycle, summarize

Two Linear behaviours shape all of it. At close Linear rolls every unfinished
issue into the next cycle, so an ended cycle's current membership is only
(roughly) the work that got done and would report 100%; the real numbers live
in the history arrays and uncompletedIssuesUponClose. And endsAt is exclusive:
a cycle ending 2026-08-16T04:00Z is over at that instant, which is exactly when
the next one starts.
"""

from __future__ import annotations

import datetime

from linear_api import gql, warn_if_capped

ADHOC = "adhoc"
DRIVEN_PREFIX = "driven:"
# Ordered worst-to-best on the autonomy climb; progress is the distribution
# moving down this list over cycles, which is why it is three values and not a
# human/agent boolean.
DRIVEN_ORDER = ["human", "agent-supervised", "agent-auto"]
# cycleUpdate rejects a longer description with INVALID_INPUT.
DESCRIPTION_MAX = 255
PLANNING_STATUSES = ("planned", "skipped")

# Separate queries on purpose: cycles-with-issues in one shot blows Linear's
# complexity budget (~12k against a 10k ceiling), and uncompletedIssuesUponClose
# inside cycles(filter:) fails "Query too complex" as well, so it is per cycle.
Q_CYCLES = """
{ cycles(filter:{team:{key:{eq:"SB"}}}, first:50){
    nodes{ id number startsAt endsAt completedAt description
           issueCountHistory completedIssueCountHistory
           scopeHistory completedScopeHistory } } }
"""

Q_MEMBERS = """
query($id:ID!){ issues(filter:{cycle:{id:{eq:$id}}}, first:250){
    nodes{ identifier title estimate createdAt
           state{name type} labels{nodes{name}} } } }
"""

Q_UNCOMPLETED = """
query($id:String!){ cycle(id:$id){ uncompletedIssuesUponClose(first:250){
    nodes{ identifier title estimate createdAt
           state{name type} labels(first:20){nodes{name}} } } } }
"""

M_DESCRIPTION = """
mutation($id:String!,$d:String!){ cycleUpdate(id:$id, input:{description:$d}){
    success cycle{ number description } } }
"""


def fetch_cycles() -> list[dict]:
    nodes = gql(Q_CYCLES)["cycles"]["nodes"]
    warn_if_capped(nodes, 50, "cycles")
    return nodes


def fetch_members(cycle_id: str) -> list[dict]:
    """Issues in the cycle NOW. For an ended cycle that is only what stayed
    behind at close, and it drifts afterwards even for completed work."""
    nodes = gql(Q_MEMBERS, {"id": cycle_id})["issues"]["nodes"]
    warn_if_capped(nodes, 250, "cycle issues")
    return nodes


def fetch_uncompleted_upon_close(cycle_id: str) -> list[dict]:
    """The set Linear rolled forward at close. Each issue comes back in its
    CURRENT state, estimate and labels, not as it was at close. Empty for a
    cycle that has not closed."""
    nodes = gql(Q_UNCOMPLETED, {"id": cycle_id})["cycle"]["uncompletedIssuesUponClose"]["nodes"]
    warn_if_capped(nodes, 250, "uncompleted-upon-close issues")
    return nodes


def parse_ts(s: str) -> datetime.datetime:
    """Linear's ISO timestamps ('...Z') as aware datetimes."""
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def days_until(ts: str, now: datetime.datetime) -> int:
    """Calendar days in now's timezone. Boundaries are local midnight, so this
    is the count a human would give."""
    return (parse_ts(ts).astimezone(now.tzinfo).date() - now.date()).days


def cycle_phase(cycle: dict, now: datetime.datetime) -> str:
    """'ended', 'active' or 'future'. End is exclusive: at the boundary instant
    the old cycle has ended and the new one is active, never both."""
    if now < parse_ts(cycle["startsAt"]):
        return "future"
    return "active" if now < parse_ts(cycle["endsAt"]) else "ended"


def select_cycle(
    cycles: list[dict], *, number: int | None = None, previous: bool = False, now: datetime.datetime
) -> dict:
    """By number, else the most recently ended (previous), else the active one.

    With no active cycle (a gap in the schedule) this falls back to the
    latest-ending cycle, as the report always has. Raises LookupError."""
    if number is not None:
        found = next((c for c in cycles if c["number"] == number), None)
        if found is None:
            raise LookupError(f"cycle {number} not found")
        return found
    if previous:
        ended = [c for c in cycles if cycle_phase(c, now) == "ended"]
        if not ended:
            raise LookupError("no ended cycle")
        return max(ended, key=lambda c: parse_ts(c["endsAt"]))
    if not cycles:
        raise LookupError("no cycles found")
    active = [c for c in cycles if cycle_phase(c, now) == "active"]
    return active[0] if active else max(cycles, key=lambda c: parse_ts(c["endsAt"]))


def previous_cycle(cycles: list[dict], cycle: dict) -> dict | None:
    """The cycle that ended when this one started, or the nearest earlier one
    if the schedule has a gap."""
    start = parse_ts(cycle["startsAt"])
    earlier = [c for c in cycles if parse_ts(c["endsAt"]) <= start]
    return max(earlier, key=lambda c: parse_ts(c["endsAt"])) if earlier else None


def _is_planning_line(line: str) -> bool:
    key, sep, _ = line.partition(":")
    return bool(sep) and key.strip().lower() == "planning"


def _line_ending(line: str) -> str:
    """What splitlines(keepends=True) left on the end of `line`, if anything."""
    return line[len(line.splitlines()[0]):]


def parse_stamp(description: str | None) -> dict:
    """Read the `Key: value` lines a cycle description carries, e.g.

        Planning: skipped -- focus set ad hoc 2026-08-16
        Carry-in: 26 issues / 81 pts from cycle 3 (100% of opening scope)

    Lines without a colon are ignored, the first occurrence of a key wins, and
    a Planning value that is neither planned nor skipped reads as unstamped
    rather than being guessed at."""
    fields: dict[str, str] = {}
    for line in (description or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields.setdefault(key.strip(), value.strip())
    raw = next((v for k, v in fields.items() if k.lower() == "planning"), None)
    status = note = None
    if raw is not None:
        head, _, tail = raw.partition("--")
        if head.strip().lower() in PLANNING_STATUSES:
            status = head.strip().lower()
            note = tail.strip() or None
    return {"planning": status, "planning_note": note, "fields": fields}


def set_planning(description: str | None, status: str, note: str | None = None) -> str:
    """Return the description with its Planning line replaced in place (or
    prepended), every other line and line ending untouched. Raises ValueError
    rather than truncating: a clipped stamp would silently lose someone's notes."""
    if status not in PLANNING_STATUSES:
        raise ValueError(f"planning status must be one of {', '.join(PLANNING_STATUSES)}, not {status!r}")
    # Any separator splitlines() honours (\v, \f, \x85, U+2028, ...) would let a
    # note forge a second `Key: value` line, not just \n and \r.
    if note is not None and len((note + "x").splitlines()) > 1:
        raise ValueError("planning note must be a single line")
    stamp = f"Planning: {status}" + (f" -- {note.strip()}" if note and note.strip() else "")
    lines = (description or "").splitlines(keepends=True)
    idx = next((n for n, line in enumerate(lines) if _is_planning_line(line)), None)
    if idx is None:
        lines.insert(0, stamp + (_line_ending(lines[0]) or "\n") if lines else stamp)
    else:
        lines[idx] = stamp + _line_ending(lines[idx])
    result = "".join(lines)
    if len(result) > DESCRIPTION_MAX:
        raise ValueError(
            f"description would be {len(result)} chars; Linear rejects more than {DESCRIPTION_MAX}"
        )
    return result


def mark_planning(cycle_id: str, description: str) -> dict:
    """The one write in this module: set the cycle description. Returns the
    cycleUpdate payload ({success, cycle{number, description}})."""
    if len(description) > DESCRIPTION_MAX:
        raise ValueError(f"description is {len(description)} chars; Linear rejects more than {DESCRIPTION_MAX}")
    return gql(M_DESCRIPTION, {"id": cycle_id, "d": description})["cycleUpdate"]


def is_adhoc(issue: dict) -> bool:
    return ADHOC in {n["name"] for n in issue["labels"]["nodes"]}


def driven(issue: dict) -> str:
    """Autonomy of delivery (SB-507). Orthogonal to planned/adhoc — an adhoc
    ticket can perfectly well be agent-delivered, so the two are reported as
    separate slices and never merged."""
    for n in issue["labels"]["nodes"]:
        if n["name"].startswith(DRIVEN_PREFIX):
            return n["name"][len(DRIVEN_PREFIX):]
    return "unlabelled"


def is_done(issue: dict) -> bool:
    return issue["state"]["type"] == "completed"


def points(rows: list[dict]) -> int:
    return sum(i["estimate"] or 0 for i in rows)


def pct(part: float, whole: float) -> int | None:
    return round(100 * part / whole) if whole else None


def _counts(rows: list[dict]) -> dict:
    return {"issues": len(rows), "points": points(rows)}


def summarize(
    cycle: dict,
    members: list[dict],
    uncompleted: list[dict] | None = None,
    prev_uncompleted: list[dict] | None = None,
    *,
    prev_cycle: dict | None = None,
    now: datetime.datetime,
) -> dict:
    """Everything the report shows, as a json.dumps-able dict. Pure.

    Which data is true is decided by Linear's close (`completedAt`), not by
    `now`: the clock only picks the cycle. A closed cycle's totals come from the
    last history sample (Linear's own numbers at close), and the planned/adhoc
    rows are rebuilt from the completed members plus the carry-out set; the rows
    use current labels and estimates, so they need not add up to the totals.
    A cycle Linear has not closed is current membership throughout, even if the
    clock says it ended (the close job runs a little after endsAt).

    Membership cannot be rebuilt as of a past `now`, so a closed cycle reads as
    ended with close data whatever `now` is. `uncompleted` is the cycle's own
    uncompletedIssuesUponClose (closed only); `prev_uncompleted` is that of
    `prev_cycle`, used for carry-in only when `prev_cycle` is closed and this
    cycle is not."""
    phase = cycle_phase(cycle, now)
    closed = bool(cycle.get("completedAt"))
    state = "ended" if closed else phase
    start, end = parse_ts(cycle["startsAt"]), parse_ts(cycle["endsAt"])
    # Until close the history arrays are running samples, not a final count, and
    # uncompletedIssuesUponClose is [] rather than the carry-out set.
    history = closed and bool(cycle.get("issueCountHistory"))
    carried = uncompleted if closed and uncompleted is not None else None

    if carried is not None:
        out_ids = {i["identifier"] for i in carried}
        done = [i for i in members if is_done(i) and i["identifier"] not in out_ids]
        population = done + list(carried)
    else:
        done = [i for i in members if is_done(i)]
        population = list(members)
    done_ids = {i["identifier"] for i in done}

    def tally(rows: list[dict]) -> dict:
        d = [i for i in rows if i["identifier"] in done_ids]
        return {
            "issues": {"done": len(d), "scope": len(rows)},
            "points": {"done": points(d), "scope": points(rows)},
        }

    planned = [i for i in population if not is_adhoc(i)]
    adhoc = [i for i in population if is_adhoc(i)]

    if history:
        totals = {
            "source": "history",
            "issues": {"done": cycle["completedIssueCountHistory"][-1], "scope": cycle["issueCountHistory"][-1]},
            "points": {"done": cycle["completedScopeHistory"][-1], "scope": cycle["scopeHistory"][-1]},
        }
    else:
        totals = {"source": "membership", **tally(population)}

    carry_out = None
    if carried is not None:
        current = points(carried)
        carry_out = {
            "issues": len(carried),
            # History is the truth at close; the carried issues' estimates have
            # been revised since, so their sum is kept alongside, not instead.
            "points": totals["points"]["scope"] - totals["points"]["done"] if history else current,
            "points_current_estimates": current,
            "planned": _counts([i for i in carried if not is_adhoc(i)]),
            "adhoc": _counts([i for i in carried if is_adhoc(i)]),
            "identifiers": [i["identifier"] for i in carried],
        }

    carry_in = None
    prev_closed = bool(prev_cycle and prev_cycle.get("completedAt"))
    if not closed and phase != "future" and prev_closed and prev_uncompleted is not None:
        prev_ids = {i["identifier"] for i in prev_uncompleted}
        rows = [i for i in members if i["identifier"] in prev_ids]
        carry_in = {
            "from_cycle": prev_cycle["number"],
            **_counts(rows),
            "planned": _counts([i for i in rows if not is_adhoc(i)]),
            "adhoc": _counts([i for i in rows if is_adhoc(i)]),
            "share_of_members": pct(len(rows), len(members)),
            "identifiers": [i["identifier"] for i in rows],
        }

    stamp = parse_stamp(cycle.get("description"))

    # Autonomy slice (SB-507). Reported over COMPLETED work only: an unfinished
    # ticket has not been delivered by anyone yet, so counting its label would
    # credit an agent for work still in Todo.
    delivered_by = {}
    for name in [*DRIVEN_ORDER, "unlabelled"]:
        rows = [i for i in done if driven(i) == name]
        if rows:
            delivered_by[name] = {"issues": len(rows), "points": points(rows), "pct": pct(points(rows), points(done))}

    # Estimates are the weak spot — unestimated work makes points meaningless.
    # `is None`, not falsy: 0 is a deliberate estimate meaning "closed as
    # superseded/duplicate, no work done", and must not be flagged as missing.
    unestimated = [
        {"identifier": i["identifier"], "title": i["title"], "adhoc": is_adhoc(i)}
        for i in population
        if i["estimate"] is None
    ]

    return {
        "schema": 1,
        "cycle": {
            "id": cycle["id"],
            "number": cycle["number"],
            "starts_at": cycle["startsAt"],
            "ends_at": cycle["endsAt"],
            "state": state,
            "closed": closed,
            "days_left": days_until(cycle["endsAt"], now) if state == "active" else None,
        },
        "totals": totals,
        "split": {
            "source": "completed membership + carry-out, current labels"
            if carried is not None
            else "membership, current labels",
            "planned": tally(planned),
            "adhoc": tally(adhoc),
        },
        "adhoc_share": {
            "issues": pct(len(adhoc), len(population)),
            "points": pct(points(adhoc), points(population)),
        },
        "created_mid_cycle": sum(1 for i in population if start <= parse_ts(i["createdAt"]) < end),
        "carry_out": carry_out,
        "carry_in": carry_in,
        "planning": {
            "status": stamp["planning"],
            "note": stamp["planning_note"],
            "stamped": stamp["planning"] is not None,
        },
        "delivered_by": delivered_by,
        "unestimated": unestimated,
    }
