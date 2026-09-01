Batch every Linear issue that needs triage prep — Triage state, or missing a
type label, an estimate, or a `driven:*` label — into ONE gated proposal, and
apply it on approval. Headless: no human at the terminal, same operating
rules as `/work-headless`. Usage: `/triage --session-id S --run-id R` (both
required — see "Session and run ids"). Every decision point calls `gate.py
open` then **exits the whole `claude -p` invocation** after printing the
output contract. Something external (cycle-runner's Monday launchd trigger,
`triage-run.sh`) resumes the same process later via `claude -p --resume S
"approved: ..."` / `"rejected: ..."` once a human has answered.

Unlike `/work-headless`, this command never branches, never opens a PR, and
never touches epic or priority — it only fills gaps in `type`/`estimate`/
`driven:*` on issues that are missing them. All the fetch/draft/apply logic
lives in `triage.py`, which imports (never forks) `pick.py`'s
`driven_label`/`policy_ok` for the autonomy policy — see that script's own
docstring for the guessing rules it uses when `type`/`estimate` are missing.

## Session and run ids

Same contract as `/work-headless`: `S` and `R` are supplied by the caller in
the invocation text, not derived — there is no tool that reports this
process's own session id. `$SESSION_ID`/`$RUN_ID` below always mean these
literal values, re-passed on the `--resume` call too.

Invoked without both flags → nothing to attach a gate to. Print
`{"status":"error","applied_count":0,"gate_id":null}` and exit 1.

`$RUN_SCRATCH` is this run's own scratch directory — derive it, don't expect
a caller to supply it:
`RUN_SCRATCH="${GATEKEEPER_STATE:-$HOME/.local/state/cycle-runner}/runs/$RUN_ID"`.
`review.md` and `apply.json` (written by `triage.py propose`) live there,
alongside `gate_id` (written below) — a resumed process is a fresh `claude
-p`, so a shell variable from phase 1 does not survive to the resume; a file
does.

## Phase 1: Propose

```bash
mkdir -p "$RUN_SCRATCH"
python3 ~/.claude/skills/cycle-runner/scripts/triage.py propose \
  --out-review "$RUN_SCRATCH/review.md" --out-apply "$RUN_SCRATCH/apply.json"
```

Read `$RUN_SCRATCH/apply.json`. If its `.changes` array is empty — nothing in
the whole SB team needs triage this week — print the output contract with
`status:"clean"`, `applied_count:0`, `gate_id:null` and exit 0. **Do not open
a gate for an empty batch**; a weekly Telegram ping with nothing to decide
trains the human to stop reading them. (A non-empty `.changes` array can
still contain entries with no fields proposed — issues already in Triage
state but otherwise fully triaged, surfaced for visibility only — that still
counts as "something to show" and opens the gate below.)

Before opening a new gate, check whether a triage gate on SB-624 is already
open. SB-624 is the fixed anchor ticket for every week's run (see below), so
if a prior gate-draining tick stalled and left last week's proposal still
`awaiting` (or `needs-human`), opening a second gate now means a single human
"approve" could resolve both at once — silently approving an older,
un-reviewed proposal alongside this one:

```bash
open_gate_id="$(python3 ~/.claude/skills/gatekeeper/scripts/gate.py status \
  | jq -r '[.[] | select(.kind == "triage" and .ticket == "SB-624"
      and (.status == "awaiting" or .status == "needs-human"))][0].gate_id // empty')"
```

If `$open_gate_id` is non-empty, log a note that this run is skipping a new
gate because one is already open on SB-624, then print the output contract
with `status:"awaiting"`, `applied_count:0`, `gate_id` set to that
`$open_gate_id` (the existing gate — never a new one), and **stop — exit the
invocation** without touching Linear or Telegram again this run.

Otherwise, open the gate. SB-624 (this very ticket) is the anchor — `gate.py`
requires exactly one `--ticket` and there is no dedicated recurring "triage
sweep" ticket, so every week's proposal lands as a comment on SB-624 itself:

```bash
bash ~/.claude/skills/linear-crud/scripts/linear.sh pack SB-624 | tee "$RUN_SCRATCH/pack.json" >/dev/null
python3 ~/.claude/skills/gatekeeper/scripts/gate.py open --kind triage \
  --ticket SB-624 --body "$RUN_SCRATCH/review.md" \
  --session-id "$SESSION_ID" --run-id "$RUN_ID" \
  --link "$(jq -r '.issue.url // empty' "$RUN_SCRATCH/pack.json")" \
  | tee "$RUN_SCRATCH/gate_open.json" | jq -r .gate_id > "$RUN_SCRATCH/gate_id"
```

Print the output contract with `status:"awaiting"`, `applied_count:0`, and
`gate_id` read back from `$RUN_SCRATCH/gate_id`. **Stop — exit the
invocation.**

## Resume

Entered when the runner calls `claude -p --resume "$SESSION_ID" "<message>"`
after `gate.py poll` resolves the gate opened above. `gate_id` for the output
contract below always comes from `$RUN_SCRATCH/gate_id`, written in phase 1.

- Message starts with `approved` (case-insensitive — `gate.py`'s own
  `parse_decision` format: `approve` / `approve: note`):
  ```bash
  python3 ~/.claude/skills/cycle-runner/scripts/triage.py apply \
    --changes "$RUN_SCRATCH/apply.json" --confirm
  ```
  `triage.py apply` re-reads every proposed issue from Linear first and
  refuses/skips any field that drifted since `propose` ran — it never
  overwrites a value a human or another run set in the meantime; see its own
  `plan()` docstring. Read the updated count from its final `N issues
  updated` line and print the output contract with `status:"applied"` and
  that `N` as `applied_count`.

- Message starts with `rejected`: write nothing, run nothing. Print the
  output contract with `status:"rejected"`, `applied_count:0`. There is no
  partial "apply the ones I liked" here — reject means the batch, as
  proposed, is wrong, same as the gate's own binary approve/reject shape.

## Output contract

Every exit — clean, awaiting, applied, rejected, or error — is exactly this
JSON and nothing after it:

```json
{"status": "clean|awaiting|applied|rejected|error", "applied_count": 0, "gate_id": "abc123 or null"}
```

Not the `branch`/`pr_url` schema `/work-headless` uses: `/triage` never
branches or opens a PR, so those fields would always be null and are omitted
rather than carried as dead weight.

## Rules that survive every phase

- Never ask a question, never wait for input.
- Never touch epic or priority — out of scope for this command entirely;
  `triage.py` never fetches or writes either field.
- Never touch a `type`/`driven:*` label that is already set, or an estimate
  that is already non-null — this command only fills gaps, never overwrites.
- Never print a secret; agent output is a permanent transcript.
