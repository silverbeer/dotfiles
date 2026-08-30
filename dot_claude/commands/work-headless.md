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
| 6 | Review | `/code-review high` | `blocked` if findings inconclusive |
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

### 2. Branch

```bash
git checkout main && git pull
bash ~/.claude/skills/linear-crud/scripts/linear.sh branch SB-<n>
```

Check `.git.dirty` from the phase-1 pack (or re-run `git status`) **before**
`git checkout main`. Dirty, or branch already exists → `blocked` gate with
`git status` output as the body, exit. A headless run never stashes or
discards to force its way past this.

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

```
/code-review high
```

**Design assumption, stated because it isn't verified:** treated as blocking
within this run — phase 7 does not start until findings text is actually
present in this turn. If it returns with nothing (rather than an explicit
"no findings"), that is **inconclusive, not clean** — `blocked` gate, body
"review did not return findings", exit. Do not treat silence as a pass. This
assumption should be confirmed against real `-p` fork behavior before this
phase is trusted unattended.

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
`gate.py --body` reads a file, never inline text:

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
- `/code-review high` returns inconclusive (see phase 6)
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
