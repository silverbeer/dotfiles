Review the active Linear cycle or plan the next one, as the product owner and scrum master for a solo developer. Usage: `/cycle review` or `/cycle plan` (no argument means `review`). Nothing is written to Linear without an explicit yes.

The numbers come from one script, and the judgement is yours. The JSON schema
is documented field by field in `~/.claude/skills/po-agent/SKILL.md`. Read it
before interpreting a field you aren't sure about.

```bash
S=~/.claude/skills/po-agent/scripts
```

## Rules that hold in both modes

- **Never recompute a number.** Run `cycle_state.py`, read its JSON, and quote
  the fields: `proposed_points`, `capacity_points`, `velocity.formula`,
  `age_days`. Do no arithmetic in prose, including "that leaves N points". To
  re-fit a plan, re-run the script with `--include SB-N` / `--exclude SB-N` /
  `--pin SB-N` and quote the new JSON. `--include` only brings a ticket into
  consideration, where it is ranked like any other. `--pin` ranks it first, in
  the order given. Neither lifts capacity.
- **Lead with the 2–3 things that matter**, not a data dump. Linear already
  has the full list. Give counts and name only the tickets that need a decision.
- **Recommend, don't survey.** Say what you would do and why, in one line each.
- **One question at a time, and only if the answer changes the plan.** The
  questions worth asking are: a priority conflict across repos (two P1s in
  different repos competing for the same room), a ticket with no acceptance
  criteria or no estimate that you want in the cycle, and stale carry-over to
  drop or re-scope. Don't ask about anything you can decide from the data.
- **No Linear write without an explicit yes.** Before any write, show the exact
  change list (ticket, field, from, to). A "no", or anything short of a clear
  yes, writes nothing. See "Writing" below.
- Link tickets with their full `url` from the JSON, never a bare identifier.
- `warnings[]` that affect a number you quote (a page cap hit, fewer closed
  cycles than the window) get one sentence each. Mention them, don't bury them.

## /cycle review

```bash
python3 $S/cycle_state.py --cycle current
```

In this order:

1. **Headline**: cycle number, `cycle.days_left`, and `summary.split` planned
   vs adhoc (done/scope, issues and points). Add `summary.carry_in` if present,
   and flag a missing `cycle.planning` stamp.
2. **At risk**: `at_risk.not_started` (planned work not begun with few days left)
   and `at_risk.stalled` (started, no movement for `days_since_activity` days).
   If both are empty, say so in one line.
3. **Waiting on you**: `waiting_on_human` with `label` and `age_days`, oldest
   first. Say which ones are **outside** this cycle (`in_cycle: false`). They
   are blocked on you all the same, but they are not cycle scope.
4. **Not ready**: `not_ready` by count, naming only the tickets that matter this cycle.
5. **1–3 recommendations**, concrete, each tied to a ticket or a field. For
   example: "answer SB-N's gate, it has waited `age_days` days", "drop SB-N from
   the cycle, it has stalled for `days_since_activity` days", "estimate SB-N".
6. Ask what the user wants to do. Any change they pick goes through "Writing".

## /cycle plan

```bash
python3 $S/cycle_state.py --cycle plan-target
```

`plan-target` decides which cycle to plan, not you. It picks the active cycle
while it is unstamped and at most 2 days in, and the next cycle otherwise.
State `plan.target.reason` in one line and don't second-guess it with date
math. If the user names a cycle, use `--cycle N --plan`.

1. **Capacity first**: quote `velocity.formula` and `plan.capacity_points`, then
   `velocity.adhoc_share_points`, because the adhoc share is why capacity is
   lower than raw velocity. If `velocity.cycles` has fewer entries than
   `params.velocity_window`, say so.
2. **The proposal**: `plan.fits` in rank order, with estimate, repo and
   `source`, and `plan.proposed_points` against `plan.capacity_points`. Mark
   carry-over, and anything `on_critical_path` or with a high `unblocks`.
3. **Projected carry-over**: if `plan.carry_over_projected` is true, say so in
   one line. The previous cycle is still running, so its open work (candidates
   with `projected: true`) is counted as carry-over for capacity, but it is not
   in the target yet. Keep it in the conversation. The change set must NOT
   contain a `cycle` move for a projected candidate; `cycle_apply.py` refuses
   one anyway. Record decisions about them (drop, re-scope, cancel at close) in
   the stamp note, or apply the cycle moves after the previous cycle closes.
   Cancel, estimate and priority changes on them are fine now.
4. **What didn't fit**: always list `plan.does_not_fit`, and never drop a
   ticket silently. If `plan.carry_over_exceeds_capacity` is true, lead with it:
   last cycle's leftovers alone exceed capacity, so something has to be
   dropped or re-scoped before anything new comes in. Recommend which.
5. **Questions**, one at a time, only ones that change the plan:
   - each `plan.needs_estimate` ticket you would otherwise include:
     "estimate SB-N? (1/2/3/5/8)"
   - a fitting ticket with `has_ac: false`: add acceptance criteria first, or take it anyway?
   - a priority conflict across repos near the capacity line
   - stale carry-over (rolled forward and idle, see `issues[].carry_in` and
     `days_since_activity`): drop it (`cycle: null`), re-scope it, or cancel it?
6. **Push back** when the user wants more than capacity. Re-run with
   `--include` to show what it would push out, and quote the new `does_not_fit`.
   Recommend what to take out rather than going along with an over-capacity
   plan. If they insist, the plan records it (say so in the stamp note).
   When the user says a ticket **must** be in the cycle, re-run with `--pin SB-N`
   (not `--include`), keeping earlier pins in their order, and quote the new
   `plan.fits` and `plan.does_not_fit`. If `plan.pinned_exceeds_capacity` is
   true, the pins alone don't fit: push back and ask which pin to drop.
7. Every answer that changes the candidate set means a re-run with
   `--include`/`--exclude`/`--pin` and a re-quote. Never adjust the list by hand.
8. When the plan settles, go to "Writing". The change set normally holds: fitting
   tickets not yet in the target (`"cycle": N`), dropped carry-over
   (`"cycle": null`, only when it is not `projected`), agreed estimates and priorities, agreed cancellations
   (`"cancel": true`), and the stamp
   `{"number": N, "planning": "planned", "note": ...}`.

## Writing

Only `cycle_apply.py` writes, and only in this sequence:

1. Show the exact change list in prose (ticket, field, from, to, plus the
   planning stamp) and ask for a yes.
2. On yes, write the changes file to a scratch path, for example
   `/tmp/cycle-<N>-changes.json`, in the shape documented in SKILL.md, then run
   the dry run and show its output verbatim:
   ```bash
   python3 $S/cycle_apply.py --changes /tmp/cycle-<N>-changes.json
   ```
3. Run `--confirm` only after an explicit yes to that dry-run output. Use one
   batch; never write ticket by ticket in conversation.
   ```bash
   python3 $S/cycle_apply.py --changes /tmp/cycle-<N>-changes.json --confirm
   ```
4. If the dry run refuses (unknown ticket or cycle, a stamp over 255 chars,
   a cycle move out of a running cycle), fix the file and show the dry run
   again. Nothing was written. Never add `--allow-active-cycle-move` unless the
   user explicitly asks to move that work out of the running cycle now.
5. After it writes, re-run `cycle_state.py --cycle <N> --plan` (not `plan-target`,
   which moves on once the cycle is stamped) and quote the new `plan.proposed_points`.

A "no" at either step means nothing is written and no changes file is run.
Ask what should change instead.
