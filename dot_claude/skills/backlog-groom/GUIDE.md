# Backlog grooming — user guide

For **you**, not the agent. (`SKILL.md` next to this file is the agent's copy —
the rubric definitions, the non-negotiables, when to escalate.)

---

## What this does

Turns an unordered Linear backlog into a single ranked list: every open issue
gets a **priority**, an **epic**, and — for anything plausibly next-cycle — an
**estimate**.

## What it deliberately does not do

- **Does not cancel anything.** Staleness is your call. The review surfaces age
  and flags anything that looks already-shipped in its rationale; deciding is
  yours.
- **Does not write to Linear until you run the apply step with `--confirm`.**
  Everything before that is files on disk.
- **Does not create epics.** If a proposed epic doesn't exist, apply refuses and
  names it. Inventing projects in your workspace isn't something a script should
  do behind you.

---

## The four phases

| # | phase | who | touches Linear? |
|---|---|---|---|
| 1 | fetch | script | reads only |
| 2 | propose epics + score | **the agent** | no |
| 3 | rank + render | script | no |
| 4 | apply | script, `--confirm` | **yes — only here** |

Phase 2 is the only part needing judgement. Phases 1, 3 and 4 are deterministic
— same input, same output, every time.

---

## Running it

### Start a groom

Just ask: *"groom the MT backlog"*, or `/backlog-groom`. Scope it by repo label
(`STK`, `MT`, `MTA`) or ask for everything.

The agent fetches, reads the whole set, proposes an epic taxonomy, scores every
issue, and hands you a review file. Expect a few minutes for ~50 issues.

### Read the review

```bash
cd ~/backlog-groom
less review.md
```

Grouped by priority, highest first. Each entry shows:

- **score** and the four components behind it
- **age** and labels
- **proposed epic** and estimate
- **changes** — exactly what would be written
- **rationale** — one line on why. This is the bit worth reading; if a rationale
  doesn't convince you, the score is wrong.

### Overrule anything

`apply.json` is the change set. It's plain JSON:

```json
{ "id": "SB-247", "priority": 2, "project": "MT Frontend Architecture", "estimate": 8 }
```

- **Change a value** — edit it in place.
- **Leave an issue alone** — delete its whole block.
- **Only apply part of it** — delete everything else.

Editing `apply.json` is the supported way to disagree. You do not need to
re-run anything after editing it.

### Create the epics first

Apply refuses until every proposed epic exists as a Linear project, and tells
you which are missing. Create them in Linear, then add a `case` arm for each to
`epic_repo()` in `~/.claude/skills/linear-crud/scripts/linear.sh` — otherwise
`linear.sh epics` lists them as `[??]`.

### Apply

```bash
# dry run — prints every change, writes nothing
python3 ~/.claude/skills/backlog-groom/scripts/apply.py --changes apply.json

# for real
python3 ~/.claude/skills/backlog-groom/scripts/apply.py --changes apply.json --confirm
```

Idempotent: it re-reads each issue and skips fields already correct. Safe to
re-run — a half-finished run just picks up where it stopped.

### Then, in Linear

Cancel whatever you flagged while reading. Pull the top of the list into the
next cycle.

---

## How the ranking works

The agent scores each issue on four axes; a script does the ranking. That split
is deliberate — asked to assign priorities directly, a model rates almost
everything "High", because in isolation every ticket sounds important.

| axis | scale | meaning |
|---|---|---|
| impact | 1–5 | user-visible value, or risk removed |
| effort | 1–5 | 1 = under an hour, 5 = multi-day |
| blocking | 0–3 | how much other work waits on it |
| decay | 0–2 | gets worse if deferred — security, drift, data loss |

```
score = 2·impact + 1.5·blocking + 1.5·decay − 0.5·effort
```

Effort is a **tiebreak, not a driver**. Cheap is a reason to do something first,
not a reason to call it important — weight it heavily and the backlog fills with
easy irrelevant work.

Sorted by score, then cut into fixed shares:

| priority | share |
|---|---|
| Urgent | 5% |
| High | 20% |
| Medium | 45% |
| Low | 30% |

**The distribution is forced on purpose.** A backlog where a third of everything
is "High" carries exactly as much information as one with no priorities at all.
Forcing the shares means "High" always names the top fifth — including after the
next groom, which is what makes runs comparable.

Consequence worth knowing: priority is **relative to the set being groomed**.
Groom only your security tickets and some of them come out Low. Groom by repo or
whole-backlog, not by a slice you already believe is important.

---

## When to run it

**Weekly, at cycle boundaries.** Cycles run Sun→Sun, so the groom rides along
with cycle planning rather than being its own event.

Also worth a run after a burst of ticket creation — a design session, a code
review, an incident.

### The weekly ritual

Pre-user, unplanned work is most of the throughput (see the `adhoc` label). So
plan each cycle at roughly **half** its nominal capacity from the groomed list,
and let adhoc absorb the rest. On a 25-point cycle that's ~12 points of backlog
work per week.

That is the whole discipline: a dozen points of chipping away, every week,
alongside whatever actually shows up. A month of it clears the security cluster
and the prod-drift work without ever fighting the adhoc flow.

At cycle close, check the `adhoc` : planned ratio. It tells you whether the
half-capacity split is right, and whether grooming is earning its keep.

### If creation outpaces throughput

Grooming re-sorts; it doesn't shrink. If you're netting more tickets per week
than you close, no grooming cadence fixes that — the lever is intake. Worth
considering an auto-cancel rule (anything untouched in Backlog for 90 days),
which forces re-creation if it still matters.

## Where this is heading

The process is deliberately manual-ish today because the rubric is unproven.
The natural progression, roughly in order of value:

1. **Staleness detection** — agents grep the repos and merged PRs to find
   tickets whose work already shipped. Genuinely parallel, needs per-issue tool
   budgets, and is the first thing worth fanning out to subagents.
2. **Auto-groom at cycle close** — the groom runs itself on the cycle boundary
   and proposes the next cycle's slate, rather than waiting to be asked.
3. **Agents working the backlog** — well-specified low-risk tickets (the 1-2
   pointers) picked up and shipped without a prompt.

Each step should wait until the one before it has been trusted for a few weeks.
An auto-groom built on a rubric nobody has sanity-checked just makes bad
decisions faster.

---

## Troubleshooting

**"REFUSING: these epics do not exist in Linear yet"** — expected on a first
run. Create them, then re-run.

**"scored issues not present in --input"** — the backlog moved since the fetch
(an issue got closed or cancelled). Re-fetch and re-rank; the agent handles this.

**"WARNING: N issue(s) were never scored"** — those are left untouched. Fine if
deliberate, otherwise ask for a rescore.

**A whole epic looks wrong** — say so rather than editing 12 entries by hand.
Re-scoring is cheap; the taxonomy step is the one most worth iterating on.

---

## Files

Everything lives in `~/backlog-groom/`:

| file | what | edit? |
|---|---|---|
| `review.md` | human-readable ranking | read-only |
| `apply.json` | the change set | **yes — this is how you overrule** |
| `scores.json` | raw rubric scores | only for a rescore |
| `groom-input.json` | backlog snapshot | no |

They're overwritten each run. Keep a copy if you want to diff two grooms.
