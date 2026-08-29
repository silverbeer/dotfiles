# Agent delivery system — design brief for external review

**Status:** built and dogfooded in a single session, 2026-08-29. Never reviewed
by anything that did not also write it.
**Audience:** a fresh reviewer with no context on this session.
**Ask:** tell us whether the shape is right before we build more on it.

---

## 1. What this is meant to be

A Linear ticket should be able to enter one end of a pipeline and come out the
other as a merged PR, with a human at the gates rather than at every keystroke.

Roles, not one agent doing everything:

| Role | Owns |
|---|---|
| **Orchestrator** (`/work`, runs in the main session) | git, Linear, gates, delegation |
| **Plan** (built-in) | codebase exploration, approach |
| **dev-engineer** | writing the code |
| **QE** (`qe-engineer` / `qe:qe-engineer`) | tests |
| **code review** (built-in `/code-review`) | finding what the others missed |

The load-bearing constraint: **only the orchestrator touches git or Linear.**
Subagents never branch, commit, push, or move a ticket. Split ownership is how
branches end up misnamed and tickets end up stuck.

### Operating context that shapes the design

- Solo developer, pre-user projects. No second human reviewer exists, ever.
- Linear (team SB) is the system of record. GitHub events drive Linear one-way.
- Work is mostly unplanned: tickets arrive daily, tagged `adhoc`, and take
  precedence over the groomed backlog.
- Dotfiles are chezmoi-managed and shared across two Macs, so agents and commands
  are user-level and load in **every** repo — ~42 repos, mostly Python, one
  Kotlin, two Node, several with no manifest at all.

### Anti-goals

- Not full autonomy. Gates are the point.
- Not a replacement for review — `/code-review` is *inside* the loop.
- Not per-repo bespoke agents. One system, resolved per repo at runtime.

---

## 2. What exists now

**Deployed to `~/.claude/` via chezmoi** (source: `~/gitrepos/dotfiles/dot_claude/`):

| Artifact | What it does |
|---|---|
| `commands/work.md` | The orchestrator. 9 phases, 4 gates. |
| `commands/ticket.md` | Light version: fetch, branch, plan, then you drive. |
| `commands/cppp.md` | Commit + PR conventions. |
| `agents/dev-engineer.md` | Implements a scoped change. No git. Inherits session model. |
| `agents/qe-engineer.md` | Tests. **pytest-specific.** Pinned to sonnet. |
| `skills/linear-crud/` | `linear.sh` wrapper: view, new, branch, list, move, link, driven, board, stats. |

**The `/work` pipeline:**

```
1 fetch+restate  2 branch ✋  3 plan ✋  4 dev-engineer  5 QE
  6 /code-review  7 fix findings  8 ship ✋  9 merge+close ✋
```

Gates (✋) are where a human says continue. Phase 3 is the important one — the
cheapest place to kill a wrong direction before three agents burn on it.

**Also shipped this session:** CI for the dotfiles repo itself (SB-904) — five
jobs, 85 tests over the checks, all pinned and checksummed. Relevant here only
because it is the first thing the pipeline built end to end.

---

## 3. Ticket ledger

| Ticket | Est | State | What |
|---|---|---|---|
| SB-903 | 3 | Done | `dev-engineer` + `/work`; fixed `/ticket` branch-naming bug |
| SB-905 | 1 | Done | `linear.sh view` missing — `/work` broke on its own phase 1 |
| SB-904 | 3→**8** | Done | CI for dotfiles; 29 files, +2780 |
| SB-907 | 1 | Open | `/work` phase 5: QE selection undefined with no stack manifest |
| SB-919 | 5 | Open | Six deferred review findings; five checks gutable while green |
| SB-920 | 2 | — | This document |

Estimate drift is itself signal: SB-904 came in at 3 and cost 8.

---

## 4. Decisions made, and why

**Roles as separate agents rather than one agent with phases.** Different
system prompts, different tool sets, different failure modes. `dev-engineer` is
told it may not touch git; `qe-engineer` is told not to return until green.

**Orchestrator runs in the main session, not as a subagent.** It needs to hold
the gates and talk to the human. Subagents can't.

**Exploration is delegated, conclusions are kept.** Phase 3 sends an
`Explore`/`Plan` agent so the codebase sweep never lands in the orchestrator's
context. This worked: the plan agent burned 57k tokens and returned a page.

**Code review wraps the built-in `/code-review` rather than a custom agent.**
One less prompt to maintain, and it is already multi-level.

**User-level, not per-repo.** Both artifacts load in every repo. Phase 5
resolves the QE agent at runtime: repo's own `.claude/agents/` wins, else match
the stack, else stack-agnostic fallback.

**Every finding becomes a ticket, never a bigger diff.** "Ticket stays the unit
of work" is a rule in the skill and was enforced five times this session.

---

## 5. What dogfooding actually showed

This is the part worth reviewing. `/work` was run on itself.

**It broke on its own first phase.** Phase 1 said `linear.sh view SB-N`. There
was no `view` subcommand — the wrapper had ten verbs and not the one that starts
every piece of work. The bug pre-existed in `/ticket`; `/work` inherited it
because the line was copied. Neither was caught by reading. Both were caught in
under a minute by running. → SB-905.

**Phase 5's rule ran out.** It says detect the stack from `pyproject.toml` /
`package.json` / `go.mod` / gradle. The dotfiles repo has 7 Python files, 10
shell scripts, and **no manifest at all**. Both documented resolution steps came
up empty and the orchestrator improvised. It got the right answer by judgement,
which means the rule wasn't doing its job. → SB-907.

**Agents disagreed with each other, and the later one was right — three times.**
- The Plan agent asserted `.chezmoiignore` is gitignore last-match-wins, so an
  exception above the wildcard is a silent no-op. `dev-engineer` tested it:
  chezmoi is **order-independent**. The orchestrator had already repeated the
  false claim to the user twice.
- The orchestrator passed the reviewer's suggested gate expression
  (`contains(needs.*.result, 'success') == false`) straight through.
  `dev-engineer` rejected it: it is true only when *no* job succeeded, so
  `[failure, success]` would still pass. Weaker than what it replaced.
- The spec told the agent to build fixtures with `git archive HEAD`. Wrong here —
  the tree was intent-to-add, so fixtures would contain no check scripts, and it
  tests the last commit rather than the edit under review.

**A `max` review found 15 holes in work two agents had called green.** Five
checks could be gutted or bypassed with the suite still at 66/66. In four cases
the file's own header claimed a stronger property than the code implemented. The
worst: a gitleaks allowlist that suppressed any real credential sharing a line
with an allowlisted pattern — on a public repo, where `SETUP.md` already contains
such a line. **The orchestrator's own spec had explicitly warned against exactly
that failure**, and the implementation defeated the instruction anyway.

**QE found that the negative tests were fake.** The canonical AWS fixture
`AKIAIOSFODNN7EXAMPLE` is dropped by gitleaks' default allowlist because it
contains `EXAMPLE`. A suite built on it would have recorded "gitleaks detects
nothing" as a pass.

**Review subagents deleted three untracked files** from the repo root
(`t2/`, `h2/`, `root.txt`). Unrecoverable — untracked. Probably QE scratch
fixtures that leaked via the `cp -R` bug, but not provably.

**The ticket sat in Backlog for nine phases.** Phase 2 creates the branch, but
only phase 8 pushes, and Linear's automation fires on push. A long run leaves the
ticket looking untouched the whole time.

---

## 6. Known gaps

- **Phase 5 no-marker case** (SB-907), plus: it keys off the *repo's* stack, not
  the *changed files'* language. A Python repo whose ticket touches only shell
  would still route to the pytest agent.
- **No suite, no story.** Two tickets this session had no tests to run. Handled
  ad hoc both times; the skill doesn't say what "skipped" means.
- **Nothing bounds a run.** SB-904 went from "add CI" to 29 files and +2780
  lines. Every step was approved, but no phase asks whether the diff has
  outgrown the ticket.
- **`driven:` label never set.** Every ticket this session is `driven:human`
  despite being agent-built. `linear.sh driven` exists; `/work` never calls it,
  so agent-vs-human attribution is silently wrong in the metrics.
- **Subagents can write outside their scratch.** Demonstrated, twice.
- **Cost is unmeasured.** One ticket consumed roughly 600k subagent tokens
  across plan, dev, QE, review and fix. Nothing tracks or budgets this.
- **No resume.** A run interrupted at phase 6 has no way back in.

---

## 7. Questions for the reviewer

1. **Is nine phases with four gates the right shape**, or is this a workflow
   engine wearing a markdown costume? What would you cut?
2. **Are the role boundaries right?** Is "dev-engineer may not touch git" the
   right constraint, or does it just move coordination cost to the orchestrator?
3. **The orchestrator relayed two wrong claims from subagents** and was corrected
   by a later subagent both times. Is there a structural fix, or is
   "agents check each other" working as intended and the reporting is the bug?
4. **What should bound a run?** Diff size, token spend, wall clock, phase count —
   or should the orchestrator simply stop and re-scope when a ticket outgrows its
   estimate?
5. **Should `/work` be one command or several?** Would separate `/plan`,
   `/implement`, `/review` composable pieces be more useful than one pipeline?
6. **What is missing entirely?** A design/architecture role before Plan? A
   security role separate from review? A docs role? Something that decides
   *whether* a ticket should be built at all?
7. **Is dogfooding the right validation strategy**, given it found two real bugs
   in one run — or does it just find bugs in the pipeline while missing whether
   the pipeline is worth having?
