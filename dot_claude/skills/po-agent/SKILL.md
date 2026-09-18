---
name: po-agent
description: Product-owner / scrum-master helpers for the SB team's Linear cycles — cycle state as one versioned JSON document (planned vs adhoc, carry-in, at-risk, waiting on a human, not ready, velocity, capacity, a capacity-bound plan), the one script that writes an approved cycle plan, and the Telegram chat with the PO (po_chat.py). Driven through the /cycle command (review, plan); read directly by the standup, runner feed and chat. Use when reviewing or planning a cycle, when something needs cycle numbers, or when posting a PO question to Telegram.
allowed-tools: Bash, Read
---

# PO agent

Driven through `/cycle review` and `/cycle plan` (`~/.claude/commands/cycle.md`),
which hold the conversation rules. This file is the reference for the two scripts
and, above all, the JSON schema every reader depends on.

Two scripts, one rule each (and `scripts/po_chat.py`, the Telegram chat, which
drives both: see [po_chat.py](#po_chatpy)):

- `scripts/cycle_state.py` is **read-only** and prints exactly one JSON document.
  The numbers are all in it. A reader quotes fields; it never re-derives a number.
- `scripts/cycle_apply.py` is the **only writer**. It does a dry run unless given `--confirm`.

Both import `linear-crud/scripts/cycles.py` (cycle arithmetic, SB-626) and
`cycle-runner/scripts/pick.py` (gate labels, priority order, the board's graph
helpers). They import these, never fork them.

## cycle_state.py

```bash
S=~/.claude/skills/po-agent/scripts
python3 $S/cycle_state.py                            # the active cycle (review)
python3 $S/cycle_state.py --cycle plan-target        # the cycle /cycle plan should plan, with `plan`
python3 $S/cycle_state.py --cycle next               # next cycle, with `plan`
python3 $S/cycle_state.py --cycle 7                  # by number
python3 $S/cycle_state.py --cycle current --plan     # add `plan` to any selection
python3 $S/cycle_state.py --cycle next --include SB-12 --exclude SB-40   # re-fit (repeatable)
python3 $S/cycle_state.py --cycle plan-target --pin SB-1088 --pin SB-989 # rank these first, in this order
python3 $S/cycle_state.py --as-of 2026-09-20T09:00:00-04:00               # evaluate at another instant
```

Options: `--at-risk-days N` (default 3), `--stale-days N` (3), `--velocity-window N` (3).
Errors go to stderr with a non-zero exit, and stdout stays empty. It makes about
11 queries (fewer for review) and needs no write scope.

`plan-target` picks the **active** cycle while it has no planning stamp and is
at most 2 days in (the boundary day and the weekend after it). Otherwise it
picks the **next** cycle. `plan.target.reason` says which rule applied.

## Schema `po-agent.cycle_state/1`

**Versioning.** Adding a field, or a new value to an enum, keeps `/1`, so
readers must ignore what they don't know. Renaming or removing a field, or
changing its meaning or type, bumps the number.

Conventions: timestamps are Linear's ISO strings (`...Z`). `priority` is
Linear's (0 none, 1 urgent … 4 low). `estimate` is `null` when missing and `0`
when deliberate (superseded / won't do). Percentages are ints or `null`. Lists
are never `null`; objects that don't apply are.

| Field | Meaning |
|---|---|
| `schema` | `"po-agent.cycle_state/1"` |
| `generated_at` | the `now` used (honours `--as-of`) |
| `team` | `"SB"` |
| `mode` | the `--cycle` value as given: `current`, `next`, `plan-target` or a number |
| `params` | `{at_risk_days, stale_days, velocity_window, include[], exclude[], pin[]}` as used |
| `cycle` | `{id, number, starts_at, ends_at, state, closed, days_left, days_elapsed, planning}`. `state` is `active`/`ended`/`future` (`ended` whenever Linear closed it). `closed` is Linear's `completedAt`. `days_left` is set only while active. `days_elapsed` is 0 on the first day, active only. `planning` is `{status: planned\|skipped\|null, note, stamped}` |
| `summary` | `cycles.summarize()` verbatim, its own `"schema": 1` (see `summarize()` in `linear-crud/scripts/cycles.py`): `totals`, `split{planned,adhoc}`, `adhoc_share` (of scope), `carry_in`, `carry_out`, `created_mid_cycle`, `delivered_by`, `unestimated`. For a closed cycle, quote `totals` (history), not `split` |
| `issues[]` | the cycle's **current** members, by issue number. See below |
| `at_risk.not_started[]` | `{identifier, title, estimate, priority}`: planned (not `adhoc`) members in triage/backlog/unstarted, listed only once `days_left <= at_risk_days` |
| `at_risk.stalled[]` | `{identifier, title, state, days_since_activity}`: members in a started state idle for `>= stale_days`, most idle first |
| `waiting_on_human[]` | **workspace-wide**, oldest first: `{identifier, title, url, label, since, age_days, age_source, in_cycle}` for open issues carrying `gate:needs-human` or `gate:awaiting-approval`. `since` is when the current label was last added (`age_source: "history"`), or `updatedAt` if the last 10 history entries don't show it (`"updatedAt"`). `in_cycle` says whether the issue is in `cycle`. Done/Canceled issues still carrying a gate label are excluded |
| `not_ready[]` | `{identifier, title, missing[]}` for open members; `missing` ⊆ `["estimate", "acceptance_criteria"]` |
| `velocity` | `{cycles[], mean_points_done, adhoc_share_points, capacity_points, formula}`. See below |
| `plan` | `null`, or the capacity-bound proposal (below) for `next`, `plan-target` or `--plan` |
| `warnings[]` | strings: page caps hit, fewer closed cycles than the window, a loop in the blocks graph, `--include` / `--pin` keys not found |

### `issues[]`

| Field | Meaning |
|---|---|
| `identifier`, `title`, `url` | |
| `state` | `{name, type}`, type is Linear's (`triage`, `backlog`, `unstarted`, `started`, `completed`, `canceled`) |
| `estimate`, `priority` | as above |
| `adhoc` | carries the `adhoc` label |
| `driven` | `human`, `agent-supervised`, `agent-auto` or `unlabelled` |
| `gate` | pending gate label or `null` |
| `project` | epic (Linear project) name or `null` |
| `repo` | repo label from `linear-crud/repos.json`, or `null` |
| `created_at`, `started_at` | |
| `last_activity`, `activity_source` | the later of `updatedAt` and `startedAt`, and which one it was. History is **not** read: Linear's rollover at close writes history without touching `updatedAt`, so a ticket that only rolled over has not moved |
| `days_since_activity` | whole days, floored |
| `carry_in` | the member was in the previous cycle's unfinished-at-close set |
| `has_ac` | description has a heading or bold lead-in naming acceptance / done when / definition of done / exit criteria, or a markdown checkbox. Descriptions are never emitted |
| `blocked_by[]`, `blocks[]` | identifiers of **open** issues in `blocks` relations |

### `velocity`

- `cycles[]` = `{number, points_done, issues_done, planned_points_done, adhoc_points_done}`
  for the last `velocity_window` cycles **closed by Linear** that ended before
  `cycle` starts, oldest first. `points_done`/`issues_done` come from Linear's
  history at close. The planned/adhoc columns use current labels and estimates,
  so they need not add up to `points_done`.
- `mean_points_done` = the mean of `points_done`, 1 decimal place.
- `adhoc_share_points` = the pooled share of delivered points that were adhoc:
  Σ adhoc / Σ (planned + adhoc), as an int %. This is **not** `summary.adhoc_share`,
  which measures scope.
- `capacity_points` = `floor(mean × (1 − pooled share))`, computed exactly, as an int.
  `null` when no cycle has closed.
- `formula` shows the calculation with its inputs, so the agent can quote it.

### `plan`

| Field | Meaning |
|---|---|
| `target` | `{number, id, starts_at, reason}` |
| `capacity_points` | `velocity.capacity_points` |
| `proposed_points` | sum of estimates that fit; never more than `capacity_points` |
| `candidates[]` | `{identifier, title, source, pinned, projected, rank, estimate, priority, repo, on_critical_path, unblocks, has_ac, fits, cumulative_points}` in rank order |
| `fits[]`, `does_not_fit[]`, `needs_estimate[]` | identifiers, rank order. Every candidate is in exactly one |
| `carry_over_exceeds_capacity` | carry-over estimates alone exceed capacity (a projection when `carry_over_projected`) |
| `pinned_exceeds_capacity` | estimates of the `--pin` tickets alone exceed capacity: push back and ask which pin to drop |
| `carry_over_projected` | the carry-over comes from a previous cycle Linear has not closed yet: it is ranked and fitted as if rolled, but is still in that cycle, so the change set must not move it |

`source` is one of four values. `carry_over` means the open members of the
previous cycle while it is still unclosed (they will roll forward), or the
target's carry-in once it has closed. `in_target` means already in the target
cycle. `backlog` means no cycle, backlog or unstarted, priority 1–2 (first 100).
`included` means added with `--include` or `--pin` from outside the other pools.
An issue found in several pools keeps the first source. `pinned` is true for a
`--pin` ticket, whatever its source. `projected` is true only for `carry_over`
while `carry_over_projected` is. A `--pin` or `--include` ticket that is Done
or Canceled is not a candidate and gets a warning. Done and canceled issues are never candidates.

Candidates are ranked by `--pin` first, in the order given on the command
line, then carry-over, then priority (none last), then
whether they are on the critical path (the longest `blocks` chain among the
candidates), then `unblocks` (the open issues each one blocks), then oldest.
Filling is greedy in rank order. A ticket that would overshoot goes to
`does_not_fit`, and the next one is still tried. Pinning changes the order, not
the capacity: a pinned ticket that doesn't fit is in `does_not_fit` too. An unestimated ticket never
fits and goes to `needs_estimate`. `cumulative_points` is the running total
after that row.

## cycle_apply.py

```bash
python3 $S/cycle_apply.py --changes /tmp/cycle-9-changes.json            # dry run: exact mutations, sends none
python3 $S/cycle_apply.py --changes /tmp/cycle-9-changes.json --confirm  # writes
```

```json
{"cycle": {"number": 9, "planning": "planned", "note": null},
 "issues": [
   {"identifier": "SB-1", "cycle": 9},
   {"identifier": "SB-2", "cycle": null},
   {"identifier": "SB-3", "priority": 2, "estimate": 3},
   {"identifier": "SB-4", "cancel": true}]}
```

- Every key except `identifier` is optional. An absent key leaves the field alone.
- `"cycle": null` removes the issue from its cycle. Dropping carry-over needs this,
  because Linear rolls unfinished issues forward at close.
- A cycle change (to a number or `null`) for an issue whose current cycle is
  still running (started and not yet closed by Linear) is **refused**, listing
  every such issue: it would pull live work out early and rewrite that cycle's
  history. Apply it after the cycle closes. Estimate, priority and cancel
  changes on those issues are allowed. `--allow-active-cycle-move` overrides
  the refusal; use it only when the user explicitly asks for the move now.
- `"cancel": true` sets the team's Canceled state **and** `estimate: 0`.
- `cycle.planning` is `planned`, `skipped` or `null` (no stamp). It is written last,
  after every issue update succeeds. A description over 255 chars refuses the whole batch.
- The script re-reads each issue first and skips fields that already match, so a
  second run is a no-op and a failed run can be re-run. Malformed input, an
  unknown issue or an unknown cycle refuses the batch before any write.

## po_chat.py

The PO, over Telegram (SB-1089). It runs as the `po-chat` Deployment
(`k3s/cycle-runner/po-chat.yaml`).

```bash
python3 $S/po_chat.py consume     # for ever: answer the gatekeeper inbox (the Deployment runs this)

# Ask a question from the pod, where the question file lands on the PVC the
# chat reads. It refuses to run anywhere else (PO_CHAT_POD), because elsewhere
# the user's reply would be silently dropped.
kubectl exec deploy/po-chat --namespace cycle-runner -- \
  python3 /work/dotfiles/dot_claude/skills/po-agent/scripts/po_chat.py ask SB-12 "Estimate SB-12? 1/2/3/5/8"
```

`consume` reads the gatekeeper inbox, never Telegram: `listen.py` is the one
`getUpdates` reader. Each message goes through these steps, in order:

1. **A reply to a PO question.** The answer goes through the runner's
   `scan_log_clean` (gitleaks, fail closed), then is posted as a comment on the
   ticket, carrying `gate.py`'s echo marker: `PO question (Telegram): …` followed
   by `Answer: …`. Each question is answered once. An answer the scan does not
   pass reaches nothing at all — not Linear, not the chat log, not the PO. A
   message that isn't a Telegram reply never becomes a comment, and a reply to a
   question never touches a pending proposal.
2. **A proposal is pending.** A plain `yes` (also `y`, `yes please`, `apply`,
   optionally ending in `.` or `!`) applies exactly the dry-run file with
   `cycle_apply.py --confirm`. `--confirm` appears in one function,
   `apply_pending`, and it is reached only when all of this holds: the dry run
   was **delivered** (`pending.json` is written after the send, with that
   message's `message_id` and `sent_at`), the yes was sent **after** it and
   within 30 minutes, by Telegram's own clock, it is not a reply to some other
   message, and the file still hashes to what was shown. Any other message,
   including `no`, discards the proposal, says "Nothing written." and goes on to
   the PO with a note. A message older than the proposal (a redelivery) leaves
   it alone.
3. **The daily budget.** If today's spend (Eastern) is at `PO_CHAT_DAILY_USD`,
   the bot says so and doesn't call `claude`. A call whose cost cannot be read —
   a timeout, a kill, unreadable output, a SIGTERM after it started — is charged
   the per-message cap.
4. **`claude -p`.** One session per cycle, `--resume`d until the cycle number
   changes, the system prompt changes, or it reaches `PO_CHAT_MAX_TURNS`. The
   prompt, on **stdin**, holds the live `cycle_state.py --cycle current` JSON, or
   a note that it is unchanged, with the day counts. There is no fallback to
   another cycle: if `--cycle current` fails, the bot says so and calls nobody.
   The system prompt is `chat.md` (the adapter) then `commands/cycle.md` (the
   rules), both read on every call. The model has no tools and no MCP servers,
   skills or settings, so its only output is JSON: `{reply, changes, ask}`,
   with `changes` schema-checked as a `cycle_apply.py` changes file. A missing
   `structured_output` is an error, never prose. A non-null `changes` is dry-run
   and kept pending for a LATER message. A non-null `ask` is posted as a
   question when its ticket resolves.

State is under `$GATEKEEPER_STATE/po-chat/`:

| path | holds |
|---|---|
| `sessions/cycle-<N>.json` | `{session_id, created_at, turns, last_state_sha, prompt_sha}`. `last_state_sha` ignores fields only the clock moves, so a day that changed nothing is not resent |
| `pending.json`, `pending-changes.json` | `{changes, sha256, proposed_at, cycle, message_id, sent_at, expires_at}` and the exact file dry-run. Written only once the dry run has been delivered |
| `questions/<telegram message_id>.json` | `{ticket, issue_id, url, question, asked_at, message_id, answered_update_id}` |
| `ledger/<YYYY-MM-DD ET>.json` | `{spent_usd, calls}` |
| `log/<YYYY-MM-DD ET>.jsonl` | `{ts, update_id, dir, text, cost, session_id, latency_s}` per message in and out |
| `notes.json` | what the PO hears on its next successful turn (a change applied, a question answered) |
| `attempts/<update_id>`, `heartbeat`, `work/` | crash retries (dropped after 2), the liveness probe, claude's cwd |

| env | default | |
|---|---|---|
| `PO_CHAT_MODEL` | `sonnet` | `--model` |
| `PO_CHAT_MSG_USD` | `0.75` | `--max-budget-usd` per call. Over it, the cost is charged and the bot says so |
| `PO_CHAT_DAILY_USD` | `10` | a day's spend, Eastern |
| `PO_CHAT_MAX_TURNS` | `30` | turns before a session is rotated, so its history cannot grow past the per-message cap |
| `PO_CHAT_POD` | set by the Deployment | `ask` refuses to run without it |
| `CLAUDE_CONFIG_DIR` | set by the Deployment | a config dir of the chat's own, so no plugin leaks in (SB-991) |

Also needs `LINEAR_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` (`cycle-runner/scripts/env.sh`)
and the two Telegram vars (`gatekeeper/scripts/env.sh`). Exit codes: `0` stopped,
`75` dotfiles@main moved (the container loop fetches and re-runs it), and
anything else is a crash.
