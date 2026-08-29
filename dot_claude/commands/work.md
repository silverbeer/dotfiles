Pick up a Linear ticket and drive it to a merged PR through role agents.
Usage: `/work SB-123` (or `/work` to pick from your open issues in this repo).

You are the **orchestrator**. You own git and Linear; the role agents own the
work inside them. Never delegate a git or Linear operation to a subagent —
split ownership is how branches end up misnamed and tickets end up stuck.

## Phases

| # | Phase | Who | Gate |
|---|-------|-----|------|
| 1 | Fetch + restate | you | — |
| 2 | Branch | you | ✋ confirm |
| 3 | Plan | `Plan` / `Explore` agent | ✋ **approve plan** |
| 4 | Implement | `dev-engineer` | — |
| 5 | Test | `qe-engineer` | — |
| 6 | Review | `/code-review` | — |
| 7 | Fix findings | `dev-engineer` | — |
| 8 | Ship | you (`/cppp`) | ✋ confirm before PR |
| 9 | Merge + close | you | ✋ confirm merge |

### 1. Fetch and restate

```bash
bash ~/.claude/skills/linear-crud/scripts/linear.sh view SB-<n>
```

No ticket given → list the user's open issues for this repo and ask which one.

Restate the acceptance criteria in 2–4 bullets. **If the description is thin,
ask one clarifying question now and wait.** A vague AC produces a vague diff and
wastes the whole chain. If the ticket has no estimate, propose one (1/2/3/5/8).

### 2. Branch

```bash
git checkout main && git pull
bash ~/.claude/skills/linear-crud/scripts/linear.sh branch SB-<n>
```

Use `linear.sh branch`, **not** a hand-rolled `git checkout -b`. The
`silverbeer/sb-<n>-<slug>` name is what triggers Linear's auto-move to
In Progress on push; any other name silently doesn't.

Working tree must be clean first. If it isn't, stop and show `git status`.

### 3. Plan — gate

Delegate exploration to an `Explore` or `Plan` subagent so the codebase sweep
never lands in your context. Come back with a plan short enough to read: files
to touch, approach, risks.

**Stop here. Show the plan and the acceptance criteria. Wait for approval.**
This is the cheapest place in the whole chain to catch a wrong direction.

For a genuinely trivial fix (one file, obvious change), say so and offer to skip
straight to phase 4 — but still show what you're about to do.

### 4. Implement

Hand `dev-engineer` the ticket key, the acceptance criteria verbatim, and the
approved plan. It writes code only — it will not touch git.

Read its "deliberately not done" section. Anything real in there gets offered to
the user as a follow-up ticket (`adhoc` label, estimate set) — offer once, don't
push.

`linear.sh new` auto-detects the repo label and **dies on an unmapped repo**
(`unknown repo '<name>' — pass --repo explicitly`); only ~14 of the repos under
`~/gitrepos` are mapped in `repo_label()`. That is not a reason to abandon the
ticket: ask the user which label to use and pass `--repo` explicitly. Don't
invent a label — it has to already exist in Linear or the create fails.

### 5. Test

**Pick the QE agent before you delegate — do not assume `qe-engineer`.** Project
agents override user-level ones *by name only*, so a repo whose guardian is
named something else gets silently ignored:

```bash
ls .claude/agents/ 2>/dev/null
```

Resolve in this order:

1. A QE-shaped agent in the repo's own `.claude/agents/` — whatever it is called
   (`qe-engineer`, `test-guardian`, …). The repo's own agent always wins.
2. Otherwise, match the stack. The user-level `qe-engineer` is **pytest-specific**
   — its conventions (`tests/test_<mod>.py`, `pytest.mark.parametrize`,
   `unittest.mock`) are wrong for anything else. For a non-Python repo use the
   stack-agnostic `qe:qe-engineer` instead.

Detect the stack rather than guessing: `pyproject.toml` → python, `package.json`
→ node, `go.mod` → go, `*.gradle*` → kotlin.

Then hand the chosen agent the changed files. It writes the missing tests and
does not return until the suite is green. If it reports red, do **not** proceed
to phase 8 — go back to `dev-engineer` with the failures.

### 6. Review

```
/code-review high
```

Reviews the working diff. Use `high` by default; `medium` for a small diff,
`max` for anything touching auth, money, migrations, or secrets handling.

### 7. Fix findings

Send confirmed findings back to `dev-engineer` as a scoped list. Re-run the test
suite after. Findings you're choosing not to fix get stated explicitly to the
user with the reason — never silently dropped.

### 8. Ship — gate

**Stop. Show the user:** files changed, test result, review findings fixed vs.
accepted. Then run `/cppp`.

- PR title references the ticket: `feat: thing (SB-123)`
- PR body **must** contain `Fixes SB-123` — that is what auto-transitions the
  ticket to Done on merge
- Never `git add .`; stage only what belongs to this ticket

### 9. Merge and close — gate

Wait for CI. Green own PR → merge it (confirm with the user first; per the house
rules you don't wait to be asked, but you do say what you're about to do).

After merge: if reality differed from the estimate, revise it on the ticket. The
drift is the signal — a ticket that came in at 2 and cost 8 is worth recording.

## Failure handling

Stop and hand back to the user — do not improvise around any of these:

- Working tree dirty at phase 2, or branch already exists
- `linear.sh` fails (run `doctor.sh` before assuming a Linear outage)
- `op://Personal/...` read failure — that is the wrong vault, never a lock.
  Do not retry, do not ask for a fingerprint. See CLAUDE.md.
- Test suite red after two `dev-engineer` round trips
- Ticket scope turns out to be materially bigger than the AC described

## What this assumes

Both `/work` and `dev-engineer` are user-level, so they load in **every** repo,
not just the ones listed here. What is *not* universal:

- **A GitHub remote**, for phases 8–9. No remote → stop after phase 7 and hand
  the user a summary of the diff instead.
- **A repo label in `repo_label()`**, for filing follow-up tickets only. The
  core path (`view` → `branch` → PR) needs no repo mapping.
- **A QE agent matching the stack** — see phase 5.

## Rules that survive every phase

- Never commit to `main`, never force push, never `--no-verify`
- Never print a secret; agent output is a permanent transcript
- Ticket stays the unit of work — new scope becomes a new ticket, not a bigger diff
