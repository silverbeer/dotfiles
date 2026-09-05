Pick up a Linear ticket and drive it to a merged PR through role agents — headless.
Usage: `/work-headless SB-N --session-id S --run-id R` (both required — see
"Session and run ids"). No human is at the terminal: never ask a question, never
wait mid-turn. Every decision point calls `gate.py open` then **exits the whole
`claude -p` invocation** after printing the output contract. Something external
(cycle-runner, SB-929) resumes the same process later via
`claude -p --resume S "..."` once a human has answered.

You are the **orchestrator**, same ownership as `/work`: you own git and Linear,
role agents own the work inside them. Never delegate a git or Linear operation.

## Session and run ids

`S` and `R` are supplied by the caller in the invocation text, not derived. There
is no tool inside this prompt that reports this process's own session id — it is
assigned by the CLI harness, not exposed to the model. The runner pre-generates a
UUID, starts `claude --session-id "$S" -p "/work-headless SB-N --session-id $S
--run-id $R"`, and re-passes both on every `--resume` call too. `$SESSION_ID` /
`$RUN_ID` below always mean these literal values. One `run_id` per ticket for its
whole lifecycle (plan/pr/merge gates share it — that's what lets a human, or
`report.py`, see three gates as one delivery run); a new attempt after
`needs-human` gets a new `run_id` and `session_id` together, not a resume.

Invoked without both flags → nothing to attach a gate to. Print
`{"status":"error","ticket":null,"branch":null,"pr_url":null,"session_id":null}`
and exit 1.

`$RUN_SCRATCH` is this run's own scratch directory — derive it, don't expect a
caller to supply it: `RUN_SCRATCH="${GATEKEEPER_STATE:-$HOME/.local/state/cycle-runner}/runs/$RUN_ID"`.
Every file this command writes (`plan.md`, `pack.json`, gate bodies) lives there.

## Phases

| # | Phase | Who | Gate |
|---|-------|-----|------|
| 1 | Fetch + restate | you | `blocked` if AC unusable or ticket not opted in |
| 2 | Branch | you | `blocked` if tree dirty / branch exists |
| 3 | Plan | `Plan`/`Explore` agent | `plan` (skipped on `agent-auto`, still posted) |
| 4 | Implement | `dev-engineer` | — |
| 5 | Test | resolved QE agent | `blocked` if red after one round trip |
| 6 | Review | `code-review.sh <worktree> high` | `blocked` on exit 3 (no verdict); `[]` is clean |
| 7 | Fix findings | `dev-engineer` | — |
| 8 | Ship | you (inline, not `/cppp`) | `pr` |
| 9 | CI + merge | you (CI poll, then gate) | `merge` (never runs `gh pr merge`) |

### 1. Fetch and restate

```bash
mkdir -p "$RUN_SCRATCH"
bash ~/.claude/skills/linear-crud/scripts/linear.sh pack SB-<n> | tee "$RUN_SCRATCH/pack.json"
```

`pack`'s issue is the brief (~300 B, **no description** — see `linear.sh:issue_brief`).
Check `.issue.labels` in `pack.json` for `driven:agent-supervised` or
`driven:agent-auto`. Neither present → `blocked` gate, body "not opted into
headless delivery", exit. This re-checks defensively even though `pick.py`'s
policy should already filter.

For the AC text itself, fetch the full issue (same as `/work` phase 1's
`view --full` fallback):
```bash
bash ~/.claude/skills/linear-crud/scripts/linear.sh view --full SB-<n>
```
Restate the AC in 2–4 bullets. **Never ask a clarifying question.** A thin
description gets restated as-is with explicit assumptions listed — the plan
gate (phase 3) is where a human corrects an assumption, not a question
mid-run. Empty description (not just thin) → `blocked` gate, body "no
acceptance criteria to act on", exit. Missing estimate → propose one inline in
the phase-3 plan body; don't stop for it.

### 2. Worktree and branch

```bash
WT="$(bash ~/.claude/skills/linear-crud/scripts/linear.sh worktree SB-<n>)"
cd "$WT"
```

**Every git and file operation for this ticket happens inside `$WT`. Never work
in `~/gitrepos/<repo>` (SB-947.)** That directory is a human's own checkout —
and for `dotfiles` it is chezmoi's source of truth, so leaving it on the wrong
commit desynchronises the deployed system from its source. On 2026-08-31 a run
left it detached four commits behind main; a routine `chezmoi apply` would have
silently reverted five merged fixes.

`linear.sh worktree` fetches, cuts the branch from `origin/<default>`, and
prints the path. It is idempotent — a resumed run gets the same tree back with
its commits intact — and it never resets an existing branch. Repos that need
gitignored dependencies (`node_modules`, `.venv`, `.env`) get them linked or
copied per `worktreeLink` / `worktreeCopy` in `repos.json`.

Do **not** also run `linear.sh branch`: `worktree` has already created and
checked out the branch. `branch` remains for interactive use in a normal
checkout.

The branch comes from `origin/<default>`, never local HEAD, so **a stale local
`main` is not a blocker and never raises a gate** (SB-946). Do not `git pull`
or `git checkout main` first — there is nothing to bring up to date, and the
primary clone is not yours to move.

Do not re-run `git status` on the primary checkout and reason about it. Its
state — dirty, behind, full of untracked scratch — is now irrelevant to this
run by construction. Reading that raw output is what once made a run stop and
wake a human over a `git fetch` (SB-946).

Gate **only** where work could actually be destroyed:

| condition | action |
|---|---|
| the ticket's branch is checked out in the primary clone (a human is on it) | `blocked` gate, exit |
| behind origin, untracked files, a dirty primary clone | proceed — a worktree is unaffected by any of it |

A fresh worktree is clean by construction, so a dirty primary checkout is no
longer a reason to stop — whatever state a human has left their own clone in
cannot reach this run. An existing ticket branch is reused with its commits
intact; neither `worktree` nor `branch` ever resets one, which is what makes a
resumed run safe. A headless run still never stashes or discards to force its
way past anything.

### 3. Plan — gate (conditional)

Delegate exploration to an `Explore`/`Plan` subagent exactly as `/work` phase 3:
files to touch, approach, risks. Write it to a scratch file. No "trivial fix,
skip ahead" shortcut here — no one is present to accept that offer, so always
produce the full plan.

```bash
plan="$RUN_SCRATCH/plan.md"   # write plan text to $plan — gate.py --body reads a FILE, never inline text
```

- **`driven:agent-supervised`** (the default headless case):
  ```bash
  python3 ~/.claude/skills/gatekeeper/scripts/gate.py open --kind plan \
    --ticket SB-<n> --body "$plan" \
    --session-id "$SESSION_ID" --run-id "$RUN_ID" --link "$(jq -r .issue.url "$RUN_SCRATCH/pack.json")"
  ```
  Print the output contract with `status:"awaiting"` and this `gate_id`. **Stop
  — exit the invocation.**

- **`driven:agent-auto`** (estimate ≤2, adhoc/chore): skip the wait, but the AC
  requires the plan still be posted. `gate.py open` has no fire-and-forget
  mode — every call creates an *awaiting* gate — so do not call it here; that
  would silently reintroduce the stop the auto path exists to avoid. Post the
  plan as a plain comment instead and continue in the same invocation:
  ```bash
  linear issue comment add SB-<n> --body-file "$plan"
  ```

### 4. Implement

Same as `/work` phase 4: hand `dev-engineer` the ticket key, AC verbatim, the
plan. Its "deliberately not done" section is not offered to anyone — carry it
into the phase-8 gate body instead ("not done: … — file as follow-up?"), never
auto-filed. `linear.sh new`'s unmapped-repo death is the same: no one to ask
which label, so note it in the ship-gate body and move on — do not block
delivery on a missing repo-label mapping.

If the diff is materially larger than the ticket's estimate suggested (the
"nothing bounds a run" gap in `/work`) — `blocked` gate, body states the
delta, exit. A headless run does not self-authorize scope growth.

### 5. Test

**Pick the QE agent before delegating — same resolution as `/work` phase 5,
reused verbatim, because it's stack detection, not human interaction:**

1. A QE-shaped agent in the repo's own `.claude/agents/` wins by name, whatever
   it's called.
2. Otherwise match the stack (`pyproject.toml`→python, `package.json`→node,
   `go.mod`→go, `*.gradle*`→kotlin): pytest-specific `qe-engineer` for python,
   stack-agnostic `qe:qe-engineer` otherwise.

Hand it the changed files. Red after one `dev-engineer` round trip (headless
allows exactly one, not two — nobody is watching a second round go by) →
`blocked` gate with the failures, exit.

### 6. Review

```bash
bash ~/.claude/skills/cycle-runner/scripts/code-review.sh "$WORKTREE" high
```

**Do NOT invoke `/code-review` as a skill from inside this turn.** That was
the original instruction and it blocked every run. Invoked in-session the
agent calls the Skill tool, which answers `Skill execution completed` — a
completion marker, not a review. The findings go somewhere this turn does not
read, phase 6 sees no text, and correctly refuses to call silence a pass.
SB-870 died there, and so would every ticket after it.

The rule was right; the mechanism was wrong. Measured 2026-09-05, run as its
own `-p` process — which is what the script does — `/code-review high`
returns a fenced ```json array in its result body: finding objects when there
is something to say, `[]` when there is not. Both are machine-readable, so
this stops depending on what a model chose to narrate.

Read the exit code, not the prose:

| exit | meaning | do |
| -- | -- | -- |
| `0`, non-empty array | findings | phase 7 |
| `0`, `[]` | reviewed, nothing found | phase 8 |
| `3` | no parseable verdict | **`blocked` gate**, body "review did not return a verdict", exit |
| `2` | `claude`/`jq` missing, bad worktree | `blocked` gate — a setup fault, say so |

**Silence is still inconclusive, never a pass.** That has not changed and must
not: exit 3 blocks. What changed is that a clean review now says `[]` out
loud, so "clean" and "did not run" are finally different things.

### 7. Fix findings

Send confirmed findings to `dev-engineer`, scoped. Re-run the phase-5 suite.
Findings chosen not to fix are stated explicitly in the phase-8 gate body with
the reason — never silently dropped just because no one's there to object.

### 8. Ship — gate (`pr`)

Do **not** run `/cppp` — its confirm-before-push framing is for a human at the
keyboard. Inline the same steps it documents:

```bash
git status --porcelain           # confirm only ticket-scoped files are dirty
git add <specific files>         # never `git add .`
git commit -m "$(cat <<'EOF'
feat: <summary> (SB-<n>)

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
git push -u origin "$(git branch --show-current)"
pr_url="$(gh pr create --title "feat: <summary> (SB-<n>)" --body "$(cat <<'EOF'
## Summary
- <what and why>

## Test plan
- [x] <what ran green>

Fixes SB-<n>
EOF
)" | tail -1)"                    # gh pr create prints the URL as its last line — capture it
echo "$pr_url" > "$RUN_SCRATCH/pr_url"   # phase 9 is a separate --resume process; a shell var doesn't cross that boundary
bash ~/.claude/skills/linear-crud/scripts/linear.sh link SB-<n>   # idempotent guard
```

Write files changed, test result, findings fixed vs. accepted, and anything
deliberately not done to `$RUN_SCRATCH/ship_summary.md` — `gate.py --body`
reads a file, never inline text — then:
```bash
python3 ~/.claude/skills/gatekeeper/scripts/gate.py open --kind pr \
  --ticket SB-<n> --body "$RUN_SCRATCH/ship_summary.md" \
  --session-id "$SESSION_ID" --run-id "$RUN_ID" --link "$pr_url"
```
Print the output contract, `status:"awaiting"`, `pr_url` set to `$pr_url`.
**Stop — exit.**

### 9. CI + merge — gate (`merge`)

Entered on resume after the `pr` gate is approved. This command establishes
whether CI is green — it does **not** hand that job to the runner, and it
**never** runs `gh pr merge` itself (`--allowedTools` on the runner's `claude -p`
call excludes `gh pr merge*` for exactly this reason — merge happens in the
runner, on an approved gate, not in Claude):

Same reduction as `cycle-runner/scripts/run.sh`'s `ci_state()` (kept
byte-identical on purpose — that script re-checks this exact query on every
merge-gate resolution rather than trusting this one): an explicit allowlist,
not "not FAILURE/PENDING". A `gh pr checks --json state` row's `state` is
either a StatusContext state (SUCCESS/PENDING/ERROR/FAILURE/EXPECTED) or a
CheckRun's running status (QUEUED/IN_PROGRESS/WAITING/REQUESTED/PENDING) or
completed conclusion (SUCCESS/FAILURE/NEUTRAL/CANCELLED/SKIPPED/TIMED_OUT/
ACTION_REQUIRED/STALE/STARTUP_FAILURE). Only SUCCESS/NEUTRAL/SKIPPED pass;
PENDING/IN_PROGRESS/QUEUED are still running; every other named state and
anything unrecognized counts as failure — never fail open on an unrecognized
state:

```bash
pr_url="$(cat "$RUN_SCRATCH/pr_url")"   # written by phase 8; this is a fresh --resume process
elapsed=0; interval=30; budget=1200   # 20 min bound — this is a poll, not a wait
while true; do
  state="$(gh pr checks "$pr_url" --json state \
    --jq '[.[].state] | if any(. as $s | ($s | IN("SUCCESS","NEUTRAL","SKIPPED","PENDING","IN_PROGRESS","QUEUED") | not)) then "failure"
           elif any(.=="PENDING" or .=="IN_PROGRESS" or .=="QUEUED") then "pending"
           else "success" end' \
    2>/dev/null || echo pending)"
  [[ "$state" != "pending" ]] && break
  elapsed=$((elapsed+interval)); [[ $elapsed -ge $budget ]] && { state=timeout; break; }
  sleep "$interval"
done
```

Write the gate body to a file first (`$RUN_SCRATCH/merge.md` or `$RUN_SCRATCH/blocked.md`) —
`gate.py --body` reads a file, never inline text.

**Every `blocked` body starts with three lines, in this order, before anything
else** (SB-946 — the first one that reached a phone was raw `git status` with
no statement of what was wanted):

```
What: <what could not be done, naming the ticket>
Why:  <the reason, in a sentence — not a command's output>
Need: <the exact command or decision required of the human>
```

`Need:` names an action, never a state — "run `git -C ~/gitrepos/mt pull`" is
an instruction, "the tree is dirty" is not. Raw command output goes **below**
these lines, never above: `gate.py` truncates the Telegram DM at
`SUMMARY_CHARS` and sends the full text to the Linear comment, so anything
after the first few lines may not reach the phone at all.


- `success` → body notes the PR URL and that CI is green; `gate.py open --kind merge
  --body "$RUN_SCRATCH/merge.md" --ticket SB-<n> --session-id "$SESSION_ID"
  --run-id "$RUN_ID" --link "$pr_url"`. Print the contract, `status:"awaiting"`,
  `pr_url` set to `$pr_url`. Stop.
- `failure` → body notes which checks failed and links `$pr_url`; `gate.py open
  --kind blocked --body "$RUN_SCRATCH/blocked.md" …` (same flags). Print the
  contract, `status:"blocked"`. Stop.
- `timeout` → same as failure, body notes CI is still running after 20 min —
  don't gate a merge on an unresolved state.

Estimate drift and closing the ticket happen after the actual `gh pr merge`,
which this process has already exited by the time it runs — put the suggested
revised estimate in the merge-gate body so the runner (or the human, from the
gate) can apply it; this command cannot see the merge happen.

## Failure handling

Every one of these is a `blocked` gate with the reason as the body, then exit —
never improvise around them, and there is no user to hand back to:

- Dirty tree at phase 2, or branch already exists
- `linear.sh` fails (`doctor.sh` first — don't assume a Linear outage)
- `op://Personal/...` read failure — wrong vault, never a lock; do not retry
- Test suite red after one `dev-engineer` round trip
- Ticket scope materially bigger than the AC described
- `code-review.sh` exits 3 — no parseable verdict (see phase 6). NOT the same
  as a clean review, which exits 0 with `[]`
- CI failure or 20-minute CI timeout (see phase 9)

## What this assumes

- **A GitHub remote**, for phases 8–9. No remote → `blocked` gate after phase 7
  instead of `/work`'s "hand the user a diff summary" — no one to hand it to.
- **A repo label in `repo_label()`** — only for follow-up filing; core path
  (`pack`→`branch`→PR) needs none.
- **A QE agent matching the stack** — see phase 5.
- **`driven:agent-supervised`/`agent-auto` already set** by whatever queued
  this ticket (`pick.py`, SB-929) — phase 1 re-checks but does not set it.

## Rules that survive every phase

- Never commit to `main`, never force push, never `--no-verify`
- Never ask a question, never wait for input, never merge (`gh pr merge*` is
  excluded from this run's tools by the runner's `--allowedTools`)
- Never print a secret; agent output is a permanent transcript
- `Fixes SB-N` always in the PR body
- Every exit — success, gate, or error — is exactly the JSON output contract
  below and nothing after it (the runner validates this via `--json-schema`):

```json
{"status": "awaiting|blocked|error", "ticket": "SB-N", "branch": "silverbeer/sb-n-...", "pr_url": "https://github.com/.../pull/N or null", "session_id": "S"}
```
