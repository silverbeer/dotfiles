---
name: backlog-groom
description: Groom a Linear backlog — propose epics, score every open issue against a fixed rubric, assign priorities with an enforced distribution, and estimate cycle candidates. Produces a review file; writes nothing until approved. Use when the user wants to triage/groom/prioritise a backlog, assign epics or projects to issues, add estimates in bulk, or asks why the backlog is unordered.
---

# Backlog groom

Turns an unordered Linear backlog into a single ranked list. Runs in three
phases: fetch (script), judge (you), rank + render (script). A separate
`apply.py` writes to Linear, and **only** after the human has read the review.

## Non-negotiables

1. **Never write to Linear in the same breath as proposing.** `rank.py` emits a
   review file and a change set; the human reads the former and then runs
   `apply.py`. A groom that silently rewrote 100 issues would be unrecoverable
   without a backup nobody has.
2. **Do not propose cancellations.** Staleness is the human's call. Surface age
   in the review and stop there.
3. **You score; the script ranks.** Never hand out P1–P4 per issue yourself.
   Asked to rate one ticket in isolation, everything reads as important, and you
   end up with forty "High" and no signal. Score against the rubric; `rank.py`
   forces the distribution.

## Phase 1 — fetch

```bash
python3 ~/.claude/skills/backlog-groom/scripts/fetch.py --repo MT --repo MTA > groom-input.json
```

`--repo` is repeatable and matches the repo *label* (`STK`, `MT`, `MTA`). Use
`--all` for the whole team. Read the counts it prints to stderr — they tell you
what the gap actually is before you start.

## Phase 2 — judge

Read `groom-input.json` yourself, in batches of 10–15 issues.

**Do not delegate this to subagents for a normal-sized backlog.** Priority is
*relative*: the quality of the output depends on one context seeing everything
at once. Splitting 45 issues across four agents produces four incompatible
scales that no amount of post-processing reconciles. Around 45–60 issues fits
comfortably in one context; fan-out only earns its cost above roughly 150, or
when a phase needs per-issue tool budgets (see *Escalation*).

### 2a. Propose the epic taxonomy — before scoring anything

Existing epics are often too few to hold the backlog. Read every title, cluster
them, and propose 5–8 epics that cover the set, each with a one-line scope.
Prefer reusing an existing epic over inventing a neighbour.

Epics are Linear *projects*. `apply.py` refuses to invent them, so any new epic
must be created in Linear first — and added to `epic_repo()` in
`~/.claude/skills/linear-crud/scripts/linear.sh`, or it will list as `[??]`.

### 2b. Score each issue

Write `scores.json`:

```json
{"issues": [
  {"id": "SB-247", "impact": 4, "effort": 4, "blocking": 2, "decay": 1,
   "epic": "Frontend Architecture", "estimate": 8,
   "rationale": "3,300-LOC god component; every frontend change pays the tax."}
]}
```

| field | scale | means |
|---|---|---|
| `impact` | 1–5 | user-visible value, or risk removed |
| `effort` | 1–5 | 1 = under an hour, 5 = multi-day |
| `blocking` | 0–3 | how much other work waits on it |
| `decay` | 0–2 | gets worse if deferred — security, drift, data loss |
| `epic` | string | from 2a; omit to leave unchanged |
| `estimate` | 1/2/3/5/8 | Fibonacci; **only** for plausible next-cycle work |
| `rationale` | one line | why — this is what the human actually reads |

Score every issue in the input. Anything unscored is reported and left alone.

Estimating all 126 issues is waste — most will never enter a cycle. Estimate
roughly the top 30 points' worth and leave the rest null.

## Phase 3 — rank and render

```bash
python3 ~/.claude/skills/backlog-groom/scripts/rank.py \
  --scores scores.json --input groom-input.json \
  --out-review review.md --out-apply apply.json
```

Weighting is `2·impact + 1.5·blocking + 1.5·decay − 0.5·effort`. Effort is a
mild tiebreak, not a driver: cheap is a reason to do something *first*, not a
reason to call it important. Distribution is forced to 5 / 20 / 45 / 30 %
Urgent / High / Medium / Low.

Hand `review.md` to the human. Stop. Do not run `apply.py` unprompted.

## Phase 4 — apply (human-gated)

```bash
python3 ~/.claude/skills/backlog-groom/scripts/apply.py --changes apply.json            # dry run
python3 ~/.claude/skills/backlog-groom/scripts/apply.py --changes apply.json --confirm  # write
```

Idempotent — re-reads each issue and skips fields already matching, so a
partial run is safely re-runnable. Refuses outright if an epic does not exist.

## Escalation — when fan-out is actually worth it

- **Staleness checking.** "Has this already shipped?" needs the repo: grep the
  codebase, search merged PRs for `Fixes SB-N`. That is per-issue tool work with
  its own budget, and it parallelises well. Batch ~10 issues per agent, never one
  agent per issue.
- **Full-estate runs** (150+ issues, several repos). Batch by repo so each agent
  keeps a coherent view, then reconcile scales in one pass before ranking.

Both need explicit opt-in from the user before spending the tokens.

## Cadence

Monthly, or when creation outpaces throughput. If the backlog grows faster than
it drains, grooming only re-sorts a growing pile — say so, and raise intake
policy (an auto-cancel-after-N-days rule) as the structural fix rather than
grooming more often.
