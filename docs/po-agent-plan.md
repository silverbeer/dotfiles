# PO Agent — execution plan

> Epic: [PO Agent](https://linear.app/silverbeer/project/po-agent-4041f90b20ca).
> Plan ticket: [SB-1091](https://linear.app/silverbeer/issue/SB-1091). Written 2026-09-16.
> **Temporary.** Delete this file in the close-out step (step 9) when the epic completes.

## How to use this doc

One ticket per session. For each step, in order:

1. Open a **fresh** Claude Code session in `~/gitrepos/dotfiles`.
2. Paste the step's prompt verbatim.
3. The session ends with a merged PR. That PR also ticks the step's box below and
   adds anything the next session needs under [Handoff notes](#handoff-notes).
4. Pull `main` before starting the next step. This doc is the handoff, so a stale
   checkout means a stale plan.

Checkpoints (◆) are human steps, not tickets. They exist because principle 1
below is the point of the epic, not a nice-to-have. Do not skip them to go faster.

## Why this epic exists

Assessment, 2026-09-16. Cycle 8 had 0 of 23 planned issues done, and every closure
was adhoc. Completion fell from 71% in C5 to 54% in C6 and 45% in C7. Cycles held
54–138 issues each. The process tickets (plan, report, groom, metrics) had gone
unstarted since August, while ~35 adhoc tickets went into runner plumbing. The
cycle-runner works, but it idles: every */30 tick exits in about six seconds,
because nothing marks tickets as agent-ready. Of the planned five stages
(triage → plan → build → PR → report), only build and PR run. Those are the two
that need a human least.

## Principles (every session holds to these)

1. **Conversation before automation.** A PO capability is used by hand before it
   is scheduled. Read-only digests may be scheduled; nothing that writes to Linear
   is scheduled in this epic.
2. **Numbers are deterministic, judgement lives in the agent.** Scripts emit cycle
   state as JSON, and the agent reads it and talks. The agent never recomputes a
   number in prose.
3. **No Linear write without an explicit yes.** Show the exact change list first.
4. **Runner is frozen** except for fatal defects. A runner bug found along the way
   gets a ticket, not a detour. SB-1048's WIP is in `git stash` on its branch.
5. **Every finding becomes a ticket in this epic**, never a bigger diff.

## Order

| # | Step | Ticket | Est | Needs |
|---|---|---|---|---|
| 1 | [x] cycle-report tells the truth | [SB-626](https://linear.app/silverbeer/issue/SB-626) | 3 | — |
| 2 | [x] `/cycle` in the terminal | [SB-1087](https://linear.app/silverbeer/issue/SB-1087) | 5 | 1 |
| 7 | [ ] Telegram long-poll listener | [SB-951](https://linear.app/silverbeer/issue/SB-951) | 5 | — |
| 8 | [ ] Chat with the PO from Telegram | [SB-1089](https://linear.app/silverbeer/issue/SB-1089) | 8 | 2, 7 |
| ◆ | [ ] **Checkpoint A:** plan cycle 9 by hand with `/cycle` | — | — | 2, on 2026-09-20 |
| 3 | [ ] Daily standup to Telegram | [SB-1088](https://linear.app/silverbeer/issue/SB-1088) | 3 | 2, A |
| 4 | [ ] Runner prefers the cycle | [SB-989](https://linear.app/silverbeer/issue/SB-989) | 3 | — |
| 5 | [ ] Triage actually runs, in-cluster | [SB-987](https://linear.app/silverbeer/issue/SB-987) | 3 | — |
| 6 | [ ] PO feeds the runner | [SB-1090](https://linear.app/silverbeer/issue/SB-1090) | 5 | 2, 4, 5 |
| ◆ | [ ] **Checkpoint B:** review cycle 10 and plan cycle 11 with the runner fed | — | — | 6, on 2026-10-04 |
| 9 | [ ] Close-out | — | — | all |

**Rows are in execution order; step numbers are kept so the prompts below still
match.** Chat (steps 7–8) was originally last, so the PO's questions would be
tested at two checkpoints first. On 2026-09-16, after step 2 shipped, it moved
to run next: the user wants to talk to the PO from Telegram as soon as possible.
Steps 7–8 depend only on step 2, so nothing breaks. The cost: chat goes live
before Checkpoint A, so its first conversations are where the PO's bad questions
surface — note them for Checkpoint A rather than tuning chat blind.

**Cycles.** Steps 1–2 are in cycle 8, and steps 3–5 in cycle 9. Nothing past that
is pre-assigned. Checkpoint A's `/cycle plan` places later steps; that is the
dogfood.

---

## Step 1 — SB-626 · cycle-report tells the truth

The PO agent's numbers come from this script. Today a closed cycle reports 100%,
and carry-over is invisible, so every plan built on it would be wrong.

```text
/work SB-626

Context: step 1 of the PO Agent epic. Read docs/po-agent-plan.md first
(principles + handoff notes). SB-1087 (/cycle, the PO agent's first capability)
will read cycle numbers from this code, so they must be true.

Scope is the ticket's three defects in
dot_claude/skills/linear-crud/scripts/cycle-report.py:
  1. closed-cycle denominator from issueCountHistory / scopeHistory, shortfall
     from uncompletedIssuesUponClose
  2. endsAt is exclusive in BOTH the --previous and active-cycle selectors
  3. carry-in / carry-out, and planned/unplanned status stamped on the cycle
     description via cycleUpdate (--mark-planned / --mark-unplanned)

Structure for reuse: SB-1087 will import this from a new cycle_state.py. Put the
queries and calculations in functions that return plain dicts, keep printing
separate. Do NOT build cycle_state.py here.

Verify on live data, not only fixtures:
  - --cycle 3 reports 11/37 issues and 48/129 pts
  - --cycle 7 matches Linear's own history (77 issues, 35 completed)
  - --previous run as if on a boundary day returns the cycle that just ended
  - active cycle (8) prints carry-in
--mark-planned / --mark-unplanned write to Linear: show me the exact change and
wait for my yes before running either against a real cycle.

Tests in the dotfiles harness (.github/tests), CI green. In the same PR: tick
step 1 in docs/po-agent-plan.md and add handoff notes for SB-1087 (function
names, JSON shapes, anything surprising about the Linear cycle fields). Revise
the estimate at close if reality differed.
```

## Step 2 — SB-1087 · `/cycle` in the terminal

The PO agent's first capability, and the one every later step reads from.

```text
/work SB-1087

Context: step 2 of the PO Agent epic — the core of it. Read
docs/po-agent-plan.md first, especially the principles and the handoff notes
from SB-626, whose cycle-report functions this builds on.

What we are building: a product-owner / scrum-master agent for a solo developer
whose Linear cycles are unplanned buckets (cycle 8: 0/23 planned done, 55
issues). /cycle review (current cycle) and /cycle plan (next cycle), as in the
ticket's AC.

Decisions to settle in the plan phase (recommendations, not orders):
  - Location: a new skill dot_claude/skills/po-agent/ holding
    scripts/cycle_state.py, plus dot_claude/commands/cycle.md. A new skill needs
    `!.claude/skills/po-agent` added to .chezmoiignore BEFORE `chezmoi add`, or
    the add reports success and syncs nothing.
  - cycle_state.py is read-only and emits ONE JSON document: membership,
    planned/adhoc split, carry-in/out, at-risk, waiting-on-human
    (gate:needs-human / gate:awaiting-approval with age), not-ready (no
    estimate, no AC), 3-closed-cycle velocity, capacity. Reuse cycle-report.py
    and board.py code — import, don't copy.
  - Every later step (standup, runner feeding, chat) reads this JSON. Design
    the shape for them, and version it.

How the PO should behave in conversation (put this in cycle.md):
  - lead with the 2-3 things that matter, not a data dump
  - recommend, don't survey; one question at a time, and only questions whose
    answer changes the plan (priority conflict across repos, ticket with no
    AC or estimate, stale carry-over to drop or re-scope)
  - push back when a plan exceeds capacity; list what didn't fit, never drop
    it silently
  - never recompute numbers — quote the JSON
  - no Linear write without an explicit yes; show the exact change list first;
    batch writes after approval

Verify live before shipping: run /cycle review against cycle 8 in this session
and hand-check the at-risk and waiting-on-you lists against Linear. Run
/cycle plan for cycle 9 as a dry run: propose, then I reject, then confirm zero
writes (linear.sh list --all before/after).

Tests in the dotfiles harness for cycle_state.py (fixture JSON), CI green.
In the same PR: tick step 2 in docs/po-agent-plan.md and add handoff notes
(JSON schema location, how to invoke, anything the standup / runner-feed /
chat steps must know).
```

## ◆ Checkpoint A — plan cycle 9 by hand (2026-09-20)

Not a ticket. This is the first real use, on the cycle boundary. It tests
whether the PO asks the right questions, so note what it got wrong.

```text
/cycle review

Context: Checkpoint A of the PO Agent epic (docs/po-agent-plan.md). Cycle 8 has
just closed. Review it, then run /cycle plan for cycle 9 with me.

Cycle 9 should include SB-1088, SB-989 and SB-987 (steps 3-5) unless capacity
says otherwise — tell me if it does.

As we go, keep a list of what the PO got wrong or made awkward: bad numbers,
questions that didn't matter, missing questions, confusing output. At the end,
propose each item as a ticket in the PO Agent epic (type, estimate, one-line
AC). File them after my yes, then tick Checkpoint A in docs/po-agent-plan.md
and record the lessons under Handoff notes, via a small PR.
```

## Step 3 — SB-1088 · daily standup to Telegram

```text
/work SB-1088

Context: step 3 of the PO Agent epic. Read docs/po-agent-plan.md first,
especially handoff notes from SB-1087 (cycle_state.py JSON) and Checkpoint A
(what the PO got wrong in real use — don't bake those mistakes into the digest).

Build: a read-only Telegram digest from cycle_state.py JSON. Weekday 08:00 ET.
On the cycle boundary it becomes the weekly report (supersedes SB-934): planned
vs delivered, carry-out, adhoc-share trend, runner activity (attempted, PRs
merged, gates opened and decision source — from run logs and gate state on the
PVC). No buttons, no Linear writes.

Where things are:
  - k3s manifests: k3s/cycle-runner/ (cronjob.yaml is the model, including its
    comments on concurrencyPolicy, deadlines, backoffLimit: 0). Reuse the
    image, Secret and PVC. Validated by .github/scripts/check-k3s-manifests.sh.
  - skills reach the pod via k3s/cycle-runner/bootstrap.sh (cloned from
    dotfiles@main each run), not baked into the image — confirm the po-agent
    skill gets synced.
  - Telegram sends: dot_claude/skills/gatekeeper/scripts/tg.py. Ticket links
    must be clickable (message entities, SB-982).
  - The k3s node is 2 vCPU and ~95% requested (SB-981): set small, honest
    requests and check the pod actually schedules.
  - Cluster health checks: executable_doctor.sh (SB-1001 section). A CronJob
    that has never fired must show up there.

The numbers are deterministic. An optional one-line recommendation can come
from `claude -p` with a budget cap; the digest must still send if that call
fails. Test that path.

Done means: I received a real digest on Telegram from the CronJob (not a
manual run), it fits one phone screen, and doctor reports the CronJob. In the
same PR: tick step 3 in docs/po-agent-plan.md, add handoff notes.
```

## Step 4 — SB-989 · runner prefers the cycle

```text
/work SB-989

Context: step 4 of the PO Agent epic. Read docs/po-agent-plan.md first. The PO
agent plans the cycle; this makes the runner build the cycle instead of the
highest-priority ticket from anywhere.

Scope is exactly the ticket: dot_claude/skills/cycle-runner/scripts/pick.py —
active-cycle tickets first, out-of-cycle after (still reachable), existing
priority/createdAt order within each group, run log says which group a pick
came from. No extra Linear round trip: the cycle comes back in the existing
query. Take the ticket's recommendation on no-cycle tickets (sort after the
cycle) unless you find a reason not to — if so, ask me.

Also visible in every pick log today: "pick candidate issues hit the first:250
cap; results may be truncated". If adding the cycle filter to the query fixes
that for free, do it; if it needs pagination, that is SB-937 — leave it.

Runner is otherwise frozen: nothing else in run.sh changes. Tests cover the
four cases in the AC. In the same PR: tick step 4 in docs/po-agent-plan.md,
add handoff notes.
```

## Step 5 — SB-987 · triage runs, in-cluster

```text
/work SB-987

Context: step 5 of the PO Agent epic. Read docs/po-agent-plan.md first. Step 6
(SB-1090) folds triage into how the PO feeds the runner, so triage must
actually run. It has never run once: its launchd plist was deployed but never
loaded, and nothing checked.

Scope is the ticket's AC. The default decision is k3s, not launchd: a weekly
CronJob in k3s/cycle-runner/ reusing the image, Secret and PVC, modeled on
cronjob.yaml. If step 3 (SB-1088) left a pattern for a second CronJob, follow it
— see handoff notes. Delete
Library/LaunchAgents/io.silverbeer.triage.plist.tmpl, and unload it on the
mini if it is loaded.

Existing code: dot_claude/skills/cycle-runner/scripts/triage-run.sh,
triage.py, tests/test_triage.py, dot_claude/commands/triage.md.

The generic half matters as much as the specific one: in
executable_doctor.sh, a scheduled job that has never fired is a FAILURE, for
any CronJob in the cluster, not only triage. That's the fourth time this
failure shape has shown up.

Done means: a real scheduled run is confirmed in a log (trigger one with
`kubectl create job --from=cronjob/...` to prove it, then confirm the schedule
itself is armed), and doctor reports it. In the same PR: tick step 5 in
docs/po-agent-plan.md, add handoff notes.
```

## Step 6 — SB-1090 · PO feeds the runner

```text
/work SB-1090

Context: step 6 of the PO Agent epic. Read docs/po-agent-plan.md first, and
all handoff notes so far. The runner idles because nothing marks tickets
agent-ready; deciding readiness is a product-owner call, so the PO makes it
with me.

Build:
  - in /cycle plan and /cycle review, a section proposing which CYCLE tickets
    are agent-ready and why: estimate within the runner's policy (see pick.py
    and docs/agentic-delivery.md "Auto scope"), acceptance criteria present,
    no open question, repo agent-ready
  - on my yes, stamp driven:agent-supervised / agent-auto via
    `linear.sh driven SB-N <value>`
  - near-ready tickets (no AC, no estimate) become questions to me in /cycle,
    not permanent driven:human
  - cycle_state.py gains the readiness fields, so the standup (SB-1088) can
    show "N tickets queued for the runner"

Constraint: in the pod only dotfiles is agent-ready today — other repos' worktrees
lack node_modules/.venv/.env (SB-997). Treat repo readiness as data (a field in
repos.json or equivalent), dotfiles-only for now. Do NOT fix SB-997 here.

Done means: I approved at least one real ticket as agent-ready through
/cycle, and the runner picked it on a live tick (kubectl logs shows the pick and
says it was an active-cycle pick, per SB-989). In the same PR: tick step 6 in
docs/po-agent-plan.md, add handoff notes.
```

## ◆ Checkpoint B — review cycle 10, plan cycle 11 (2026-10-04)

```text
/cycle review

Context: Checkpoint B of the PO Agent epic (docs/po-agent-plan.md). Cycle 10
just closed. It is the first full cycle with the daily standup running and the
PO feeding the runner.

Review cycle 10, then plan cycle 11 with me. Specifically answer, from data:
  - planned completion vs cycles 7-9: did it move?
  - how many tickets did the runner pick and merge, per working day?
    (success measure: >= 1)
  - which standup digests did I act on, and which were noise?
  - what questions did the PO ask that I'd rather it had decided alone, and
    what did it decide that it should have asked?

Remaining epic work is SB-951 and SB-1089 (Telegram chat); place them in cycle
11 if capacity allows. Propose any fixes as tickets in the epic, file on my yes,
then tick Checkpoint B in docs/po-agent-plan.md with the lessons under Handoff
notes, via a small PR.
```

## Step 7 — SB-951 · Telegram long-poll listener

```text
/work SB-951

Context: step 7 of the PO Agent epic. Read docs/po-agent-plan.md first. This
is the prerequisite for chatting with the PO from Telegram (SB-1089): one
always-on process that owns getUpdates and answers button taps within seconds.

The ticket was written in the launchd era. Its options still hold, but the
runtime is now k3s, so the natural shape is a single-replica Deployment
(strategy Recreate, so two pods can never poll at once) reusing the image,
Secret and PVC, not a launchd agent. Re-decide in the plan phase and say why.

Hard invariants:
  - exactly ONE getUpdates reader per bot token (tg.py raises
    TelegramConflict on 409). run.sh's in-tick `gate.py poll` must stop
    polling Telegram and read recorded decisions instead — this is a runner
    change, allowed because it is required, keep it minimal
  - decisions stay durable when the listener is down (SB-950 semantics), and
    the runner still sees them after it comes back
  - route by update type: callback_query goes to gates now; free-text messages
    get recorded somewhere SB-1089 can consume (design that seam, don't build
    the chat)
  - the node is 2 vCPU and ~95% requested (SB-981): an always-on pod needs a
    real, small request

Code: dot_claude/skills/gatekeeper/scripts/{tg.py,gate.py},
dot_claude/skills/cycle-runner/scripts/run.sh, k3s/cycle-runner/,
executable_doctor.sh (must report the listener is running).

Done means: a real button tap on a real gate is acknowledged on my phone within
seconds, no 409s in the listener logs over a day, doctor reports it. In the
same PR: tick step 7 in docs/po-agent-plan.md, add handoff notes (the message
seam especially).
```

## Step 8 — SB-1089 · chat with the PO from Telegram

```text
/work SB-1089

Context: step 8 of the PO Agent epic. Read docs/po-agent-plan.md first — all
handoff notes, especially SB-951's message seam and the checkpoint lessons
about which questions the PO should ask versus decide alone.

Build: free-text Telegram messages from the allowlisted user reach a PO agent
session and get a reply in the same chat. The PO has live cycle_state.py JSON
and the conversation so far (one resumed session per cycle). It can ask as
well as answer: open questions from /cycle and the standup get posted, and my
reply lands as a comment on the relevant ticket. Behaviour rules are the same
as dot_claude/commands/cycle.md — reuse that text, don't fork it.

Hard rules:
  - any Linear change from chat is an exact change list plus explicit yes;
    "no" or silence writes nothing
  - per-message and per-day budget caps on `claude -p`; over cap, the bot says
    so instead of dropping the message
  - gitleaks-scan anything before it is posted to Linear (the runner already
    does this for run logs — reuse it)
  - interactive plugins must not leak into this headless session (SB-991)

Done means, on my phone: "what's at risk this cycle?" gets a correct answer
from live data within about a minute. A PO question about a ticket, answered in
chat, shows up as a comment on that ticket. A proposed change isn't written
until I say yes. In the same PR: tick step 8 in docs/po-agent-plan.md.
```

## Step 9 — Close-out

```text
Close out the PO Agent epic
(https://linear.app/silverbeer/project/po-agent-4041f90b20ca).

1. Confirm every step and checkpoint in docs/po-agent-plan.md is ticked and
   every epic ticket is Done or Canceled; list anything still open and stop if
   there is any.
2. Measure against the epic's success measures, from data: planned completion
   trend since cycle 8, cycle scope vs capacity at plan time, runner picks per
   working day. Report them to me.
3. Move anything durable from the Handoff notes into its permanent home
   (skill SKILL.md files, dot_claude/commands/cycle.md, or
   docs/agentic-delivery.md's status log). Then delete docs/po-agent-plan.md.
4. Update docs/agentic-delivery.md's plan-vs-built table: triage, plan and
   report now run.
5. File a ticket for the close-out, then branch, PR and merge. Mark the epic
   Completed in Linear after the merge, on my yes.
```

---

## Handoff notes

Each session appends here: what the next session must know that isn't in the
code or the ticket. Keep entries short; date and ticket each one.

- 2026-09-16 · SB-1091 — Superseded and canceled as duplicates: SB-933, SB-504,
  SB-505 (→ SB-1087) and SB-934 (→ SB-1088). Their descriptions still hold useful
  detail: SB-505 (groom metrics) and SB-933 (capacity formula).
- 2026-09-16 · SB-1091 — SB-1048's k3s watchdog WIP is stashed
  (`SB-1048 WIP: watchdog.sh`) on `silverbeer/sb-1048-…`. It's unrelated to this
  epic; resume it after.
- 2026-09-16 · SB-1091 — `repos.json` maps `po agent*` to DOT, so
  `linear.sh new --epic "PO Agent"` works.
- 2026-09-16 · SB-626 — **Import `cycles.py`, not `cycle-report.py`.** Everything
  lives in `dot_claude/skills/linear-crud/scripts/cycles.py`, next to
  `linear_api.py`. It never prints or exits and raises `LookupError`/`ValueError`.
  `cycle-report.py` is only the CLI. The pure function that says where a cycle
  is in time is named `cycle_phase()`, so it can't be mistaken for your
  `cycle_state.py`. Flow:
  `fetch_cycles()` → `select_cycle(cycles, number=|previous=, now=)` →
  `fetch_members(id)`, then `fetch_uncompleted_upon_close(id)` (ended cycle) or
  `previous_cycle()` + its uncompleted set (active cycle) →
  `summarize(cycle, members, uncompleted, prev_uncompleted, prev_cycle=, now=)`.
  Pass `now` as an aware datetime every time; `--as-of` is how the CLI tests
  boundaries.
- 2026-09-16 · SB-626 — **`summarize()` JSON, `"schema": 1`**
  (`cycle-report.py --json` prints it). Keys: `cycle{id,number,starts_at,ends_at,
  state,closed,days_left}`, `totals{source,issues{done,scope},points{done,scope}}`,
  `split{source,planned,adhoc}` (same shape as totals), `adhoc_share{issues,points}`,
  `created_mid_cycle`, `carry_out` (closed only:
  `{issues,points,points_current_estimates,planned,adhoc,identifiers}`),
  `carry_in` (only for a cycle that is not closed and not future, when the
  previous cycle is closed: `{from_cycle,issues,points,planned,adhoc,
  share_of_members,identifiers}`), `planning{status,note,stamped}`,
  `delivered_by{name:{issues,points,pct}}`, `unestimated[{identifier,title,adhoc}]`.
  Percentages are ints or `null`.
- 2026-09-16 · SB-626 — **Cycle fields are not what they look like:**
  - After close, an ended cycle's *membership* drifts, even for completed work
    (C3 is 10/46 today; Linear's history says 11/48). For an ended cycle,
    `totals` come from the last entry of `issueCountHistory` /
    `completedIssueCountHistory` / `scopeHistory` / `completedScopeHistory`.
    The `split` rows use today's labels and estimates, so they need not add up
    to `totals`. Quote `totals` as the truth.
  - `uncompletedIssuesUponClose` returns each issue's *current* state, estimate
    and labels. C3's set sums to 71 pts; the shortfall at close was 81. That's
    why `carry_out` has `points` (history) and `points_current_estimates`.
    States are current too: many issues in the set have since completed.
  - Asking for `uncompletedIssuesUponClose` inside `cycles(filter:)` fails with
    "Query too complex". Query it per cycle via `cycle(id:)`.
  - The history arrays are daily samples, and the active cycle's last entry
    lags about a day. Use membership for the active cycle, as `summarize` does.
  - `endsAt` is exclusive and falls on local midnight Eastern (`04:00Z`).
  - **The clock and Linear's close are separate things.** `select_cycle` /
    `cycle_phase` go by the clock. `summarize` decides where its data comes
    from using `completedAt`: when it is set, history and carry-out are used
    whatever `now` is. When it is null, current membership is used, even if
    the clock says the cycle has ended. Linear's close job runs a few seconds
    to a minute after `endsAt`, and during that gap
    `uncompletedIssuesUponClose` is `[]`, not the real set. `--as-of` affects
    only which cycle is selected; it can't rebuild what a cycle's membership
    used to be.
  - `Cycle.description` is capped at 255 chars. The stamp is `Key: value`
    lines; `Planning: planned` or `Planning: skipped -- <note>` (C3 and C4 were
    stamped by hand). `set_planning()` replaces only that one line and raises
    rather than truncate. `cycle-report.py --mark-*` is a dry run unless given
    `--yes`.
- 2026-09-16 · SB-1087 — **Where it lives.** Skill `dot_claude/skills/po-agent/`
  (`scripts/cycle_state.py` read-only, `scripts/cycle_apply.py` the only writer),
  command `dot_claude/commands/cycle.md` (the conversation rules — SB-1089 reuses
  this text, don't fork it). **The JSON schema reference is
  `dot_claude/skills/po-agent/SKILL.md`**, field by field; a test
  (`SkillMdSchema` in `.github/tests/linear_crud/test_cycle_state.py`) fails if
  the doc and the emitted keys drift. Schema id `"po-agent.cycle_state/1"`:
  adding a field or enum value keeps `/1`, renaming or removing bumps it.
  `summary` is `cycles.summarize()` verbatim with its own `"schema": 1`.
- 2026-09-16 · SB-1087 — **How to invoke.**
  `python3 ~/.claude/skills/po-agent/scripts/cycle_state.py --cycle current|next|plan-target|N`
  (`--plan` adds `plan` to any selection; `--include/--exclude/--pin SB-N`
  re-fit; `--as-of` for boundaries). ~9 queries for review, ≤12 for a plan;
  stdout is only the JSON, errors go to stderr with non-zero exit. Writes:
  `cycle_apply.py --changes file.json` is a dry run; `--confirm` writes,
  idempotently, one `issueUpdate` per issue, stamp last; a mid-batch failure
  stops and re-running the same file finishes the job.
- 2026-09-16 · SB-1087 — **For the standup (SB-1088):** quote `summary.totals`,
  `summary.split`, `summary.carry_in`, `at_risk`, `waiting_on_human`,
  `velocity.capacity_points`. `waiting_on_human` is **workspace-wide**, each
  entry flagged `in_cycle` — on 2026-09-16 the only open gate (SB-990) was
  outside every cycle. Done/canceled tickets that still carry a gate label
  (SB-964) are excluded. Gate age comes from issue history `addedLabels`
  (`age_source: history`), falling back to `updatedAt`.
- 2026-09-16 · SB-1087 — **For chat (SB-1089):** the PO answers from the JSON, so
  "what's at risk?" is `at_risk.not_started` + `at_risk.stalled`. Stalled means
  started and `max(updatedAt, startedAt)` older than `--stale-days` (3):
  Linear's automatic cycle rollover adds history but does **not** bump
  `updatedAt`, so it never counts as movement. `not_started` stays empty until
  `days_left ≤ --at-risk-days` (3) — cycle 8 had 20 unstarted planned tickets and
  an empty list on day 3. Checkpoint A hasn't happened, so there are no
  checkpoint lessons yet; note what the chat PO gets wrong for it.
- 2026-09-16 · SB-1087 — **For runner feeding (SB-1090):** add readiness to
  `issues[]` (it already has `has_ac`, `estimate`, `repo`, `driven`, `gate`,
  `blocked_by`, `blocks`). `has_ac` matches `## Acceptance`, `Done when`,
  `Definition of done`, `Exit criteria` headings or any `- [ ]` checklist;
  descriptions are not emitted. "Planned" means only "no `adhoc` label", so
  long-lived tickets (SB-100) count as planned.
- 2026-09-16 · SB-1087 — **Capacity and plan, as first seen live.** Capacity 55 =
  `floor(163.0 mean pts over C5–C7 × (1 − 322/489 adhoc pts done))`. Carry-over
  alone (40 issues, 139 pts) exceeds it, and carry-over ranks first, so SB-1088 /
  SB-989 / SB-987 (already in cycle 9) fell into `does_not_fit`. `--include` only
  adds a ticket to the ranking; **`--pin` ranks it first** (still capacity-bound,
  `plan.pinned_exceeds_capacity` when pins alone overflow). With those three
  pinned, all fit. While the previous cycle is still running, carry-over is
  `projected` (`plan.carry_over_projected`): it counts toward capacity, but
  `cycle_apply.py` **refuses** to move any issue out of a started, unclosed
  cycle (`ActiveCycleMove`; escape hatch `--allow-active-cycle-move`, only on an
  explicit ask). Drop decisions made before close go in the stamp note and are
  applied after Linear rolls the work forward (`"cycle": null` then).
- 2026-09-16 · SB-1087 — **"A rejected plan writes nothing" needs two checks.**
  `linear.sh list --all` has no cycle column and lists only your assigned
  issues, so it can't see a cycle move. Also compare cycle membership
  (`cycle_state.py --cycle N` `issues[]` before/after) and `cycle.planning`.
  Both were identical after the cycle 9 dry run was rejected.
- 2026-09-16 · SB-1087 — Local ruff is 0.12.12 but `.ruff.toml` requires
  0.16.5, so `test_ruff.sh` fails on this machine regardless of the diff. CI is
  the source of truth until the local ruff is upgraded.
