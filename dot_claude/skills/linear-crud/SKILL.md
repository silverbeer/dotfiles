---
name: linear-crud
description: Manage Linear issues from chat via the `linear` CLI — create, list, move, link, and audit issues in the silverbeer (SB) workspace, with filtering by repo and by epic (Linear project). Use when the user wants to file a ticket, check their open issues, filter issues by epic/project, list epics, see workspace stats / velocity / momentum, change an issue's state, link a PR to an issue, or find unassigned issues. Works in any silverbeer repo on any machine.
allowed-tools: Bash, Read
---

Wraps the already-installed, already-authed `linear` CLI so issue ops are one tool call instead of drafting markdown for the user to paste. Linear is the canonical tracker for all silverbeer repos; the `SB` team holds every project, scoped by a `repo` label group.

The helper lives next to this file: `scripts/linear.sh`. Run it with `bash`. It bakes in the conventions below so you don't have to reconstruct them each time.

## Non-negotiable conventions

1. **Every issue is assigned to `silverbeer.io`. Never leave one unassigned.** The helper does this automatically on `new`; for any raw `linear` call you make, pass `--assignee "silverbeer.io"`.
2. **Every issue gets a `repo` label and a `type` label** (both are mutually-exclusive groups). The helper sets these on `new`. A ticket without them is incomplete (SB-74 shipped label-less before this skill existed — don't repeat that).
3. **Confirm before any write.** For `new` and `move`, show the user the exact title / type / repo / epic / area labels / target state you're about to apply and wait for a yes. Reads (`list`, `epics`) run immediately.
4. **Every epic maps to a repo (its required "project-level label").** Epics are Linear *projects*. The helper holds the epic→repo map in `epic_repo()`; an epic with no mapping is rejected. When you pass `--epic`, the repo label is derived from it — so `--repo` is usually unnecessary. An epic may nonetheless **span repos**: `Podtelemetry — Run Audio` defaults to `POD` (the service) but holds `STK` integration tickets. Passing an explicit `--repo` overrides the epic's default with a warning rather than failing.

### Label vocabulary

- `repo` (pick one, required): `MT` missing-table · `MTA` missing-table Android app · `BOOT` missingtable-platform-bootstrap · `MS` match-scraper · `MSA` match-scraper-agent · `QB` qualityplaybook · `STK` myrunstreak · `JT` janitor · `DOT` dotfiles · `TODO` todo (github.com/silverbeer/todo) · `TRD` trd (investment tracker) · `POD` podtelemetry (run-audio capture service). Auto-detected from the current git repo; pass `--repo` to override, or `--epic` to derive it from the epic.
- `type` (pick one, required): `bug` · `feature` · `chore` (maintenance/refactor, no behavior change) · `docs` · `infra` (CI/k8s/helm/terraform) · `security`.
- `driven` (pick one, auto-applied): `driven:human` · `driven:agent-supervised` · `driven:agent-auto`. **Autonomy of delivery, not whether an LLM touched the code** — every commit in every silverbeer repo already carries `Co-Authored-By: Claude`, so authorship cannot tell the two apart. `linear.sh new` stamps `driven:human` unless `--driven` says otherwise; an agent promotes its own ticket with `linear.sh driven SB-N agent-supervised` when it opens the PR.
- area (flat, optional, multi): e.g. `backend`, `frontend`, `db`, `auth`, `qop`, `scraper-integration`. Add with repeated `--label`.

### Epics (Linear projects)

Linear has no native "Epic" — its **Project** is the epic. Run `bash scripts/linear.sh epics` to list them with the repo each maps to. Pass the exact name to `--epic`. Every epic must resolve to a repo in `epic_repo()`; to add a new epic, add a case there (this is the "project-level label" requirement). Current epics are all `STK` except `trd — Investment Tracker` (`TRD`), `MT Android App` (`MTA`) and `Podtelemetry — Run Audio` (`POD`).

### CLI gotchas (linear 2.0.0, verified 2026-06-20 — the helper targets this)

This build differs sharply from the 2026-05-30 surface — the flags flipped back. Do **not** trust older notes.

- Subcommand is `issue` (**singular**); `issues` (plural) just dumps usage and fails.
- `issue create` takes `-t/--title`, `-l/--label` **repeated** (NOT a comma list), `--description` or `--description-file`, `--project "<epic>"`, and **has** `--no-interactive`. The helper uses all of these.
- **Labels are validated against their group**: `repo`/`type` are mutually-exclusive groups — exactly one of each. `docs` lives in the `type` group (not an area label). Two type labels → `labelIds not exclusive child labels`.
- `issue mine` / `issue list` / `issue l` are **the same command** — they all list *my* issues, not the team's. Needs `--team`, requires `--sort manual|priority` (no default), and has **no `--assignee` or `--json/-j`** — text only. Open states = `--state unstarted --state started`; `--all-states` for everything.
- `issue query` is the real query engine across **all** assignees: `--assignee`, `-U/--unassigned`, `--all-teams`, `--search`, `--project`, `-l/--label`, `--all-states` (its default), and `-j/--json`. The helper's audit uses `issue query --team SB -U`.
- `issue update <id>` uses `-t/--title`, `-s/--state` (name or type), `-a/--assignee`, `-l/--label`, `--project`, `--description`/`-file`.
- `project list --team SB [-j]` lists epics (the helper parses the `-j` JSON: `.nodes[].name`); `project view <slug>` shows one.

## Commands

Run from inside the relevant repo so repo-detection works (or pass `--repo`).

### List epics — `/linear epics`
```bash
bash scripts/linear.sh epics    # Linear projects + the repo each maps to
```

### Stats — `/linear stats [--days N]`
Momentum dashboard: lifetime shipped/open/canceled, velocity over the last N days (default 7), avg open-ticket age, oldest open, approx ship time, per-repo + per-epic breakdown, a 14-day created-per-day sparkline, and a motivational closer. Read-only — runs immediately.
```bash
bash scripts/linear.sh stats            # last 7 days
bash scripts/linear.sh stats --days 30  # last 30 days
```
Computed from `issue query --all-states -j`. **Caveat:** this CLI has no `completedAt`, so "shipped in window" and "avg ship time" use `updatedAt` as the close-time proxy — directional, not exact.

### DORA metrics — `bash scripts/metrics.sh [--days N]`
Capacity + DORA-style delivery metrics (SB-360). Unlike `stats`, this uses the **Linear API** (`linear-gql.sh`) for real `createdAt/startedAt/completedAt` → true lead time + cycle time, plus deploy frequency (merged-PRs-to-main proxy via `gh search prs`) and a revert/hotfix change-failure proxy. MTTR is not implemented (no incident tracking yet). Read-only.
```bash
bash scripts/metrics.sh --days 30
```

### Cycle report — `python3 scripts/cycle-report.py [--cycle N | --previous]`
Planned vs **adhoc** split for a cycle: issues and points, completion per stream,
adhoc share, and how many issues were created mid-cycle. Read-only.

```bash
python3 scripts/cycle-report.py              # active cycle
python3 scripts/cycle-report.py --previous   # the one that just ended
```

The portfolio is pre-user, so unplanned work is most of the throughput — the
`adhoc` label exists to measure that, not to scold it. Run this at every cycle
boundary and plan the next cycle at roughly `100% − adhoc share` of capacity.

Two things to watch in the output:
- **Unestimated issues make the point totals lie.** The report names them; fill
  them in before trusting the ratio.
- **Adhoc typically completes at a higher rate than planned work** — it jumps the
  queue by definition. If planned completion is much lower, the cycle was
  over-committed, not the team under-delivering.

### Create — `/linear new <free text>`
Infer a concise title, a short markdown description, and the `type` from the conversation. Detect `repo` from cwd (or pass `--epic` to derive it). Then **show the user the proposed title + labels + epic and confirm**, then file:

```bash
# write the description to a temp file (markdown-safe), then:
bash scripts/linear.sh new --title "Roster CSV import rejects BOM" \
  --type bug --label frontend --body-file /tmp/issue.md

# attach to an epic (project) — repo label is derived from the epic:
bash scripts/linear.sh new --title "Streak heatmap legend" \
  --type feature --epic "Goals & Multi-Metric Tracking" --body-file /tmp/issue.md
```

Every issue also gets a `driven` label; it defaults to `driven:human`, which is correct for
anything filed from an interactive session. Pass `--driven agent-supervised` (or set
`LINEAR_DRIVEN`) only when an agent filed the ticket unprompted.
The command prints the new `SB-N` URL — relay it. (`--repo` is optional; omit to auto-detect.)

**Always set an estimate.** `linear.sh new` has no `--estimate` flag, so set it
immediately after filing:

```bash
bash scripts/linear-gql.sh 'mutation { issueUpdate(id: "<uuid>", input: { estimate: 3 }) { success } }'
```

Fibonacci 1/2/3/5/8. A first guess is fine — revise at close if reality
differed. Use `0` for anything closed as superseded, duplicate or won't-do, so
velocity doesn't count work nobody did (`cycle-report.py` distinguishes a
deliberate 0 from a missing estimate).

Unestimated tickets are not a cosmetic gap: on 2026-07-28 backfilling 18 of them
moved the cycle from an apparent 30 points to an actual 73, and adhoc's share of
points from 34% to 58%. Point totals without estimates understate throughput by
more than half and make capacity planning worthless.

### List — `/linear list [--all] [--epic E]`
```bash
bash scripts/linear.sh list                          # my open issues in the current repo
bash scripts/linear.sh list --all                    # include done/canceled
bash scripts/linear.sh list --epic "Local Agent Automation"        # open issues in one epic
bash scripts/linear.sh list --epic "Local Agent Automation" --all  # all states in that epic
```

### Move — `/linear move SB-N <state>`
States: `Backlog → Todo → In Progress → In Review → Done` (or `Canceled`). Confirm, then:
```bash
bash scripts/linear.sh move SB-42 "In Progress"
```

### Autonomy — `/linear driven SB-N <value>`
Re-stamp who drove the work. `driven` is a mutually-exclusive group, so this rewrites the
issue's whole label set (via `set-driven.py`) rather than appending — the CLI's `-l` would
fail with `labelIds not exclusive child labels`. Idempotent.

```bash
bash scripts/linear.sh driven SB-42 agent-supervised   # agent built it, human merges
bash scripts/linear.sh driven SB-42 agent-auto         # agent delivered + QE verified it
```

**When an agent should call this:** at PR-open time, on its own ticket. Tickets are filed
`driven:human` by default, so an agent that forgets to promote its work under-reports
itself — which is the safe direction to fail.

Both `cycle-report.py` and `metrics.sh` slice completed work by this label. The slice is
over *completed* issues only: an unfinished ticket has not been delivered by anyone yet.
It is orthogonal to `adhoc` — an adhoc ticket can be agent-delivered, and the two are
never merged into one number.

### Link — `/linear link SB-N [PR#]`
Adds `Fixes SB-N` to a PR body (idempotent). Defaults to the current branch's open PR:
```bash
bash scripts/linear.sh link SB-42        # current branch's PR
bash scripts/linear.sh link SB-42 420    # explicit PR number
```

### Audit unassigned — `/linear audit`
Find (and optionally fix) issues with no assignee — backs convention #1:
```bash
bash scripts/linear.sh audit-unassigned         # list only
bash scripts/linear.sh audit-unassigned --fix   # assign all to silverbeer.io
```

## Anything not covered

Drop to the raw CLI (`linear issue ...`, **singular**) but keep conventions #1–#4. Useful raw recipes:
- View one issue: `linear issue view SB-N`
- List labels (to confirm group membership): `linear label list --team SB`
- Add a comment: `linear issue comment add SB-N --body "text"` (or `--body-file /tmp/c.md` for markdown)
- Unassigned set: `linear issue query --team SB --unassigned --all-states` (or `--assignee <user>` for someone specific; add `-j` for JSON).

For anything the CLI can't do (initiatives, cycles, estimates, team settings, metrics) use the raw GraphQL wrapper `scripts/linear-gql.sh` (reads a personal API key from `~/.config/linear/gql-key`; supports variables as `$2`). The CLI also has a native `linear api '<query>'` that uses its own keychain auth. Verify the whole setup with `scripts/doctor.sh`.

## The delivery loop (paved road)

State transitions are **automatic** via the connected GitHub integration — do NOT `move` manually for these:

| Git event | → Linear state |
|-----------|----------------|
| branch pushed (name contains `sb-N`) | In Progress |
| PR marked ready for review | In Review |
| PR merged | Done |

So the loop is:
```
linear.sh branch SB-N     # checkout silverbeer/sb-n-<slug>  → auto: In Progress
# …implement + test…
/cppp                     # commit, push, open PR (body: "Fixes SB-N")  → auto: In Review
# merge the PR            #                                             → auto: Done
```
`linear.sh move` is only for grooming (Backlog→Todo) or exceptions. Branch names **must** contain the issue id — `linear.sh branch` guarantees it.

**Still open (SB-18):** first-class sub-issues in the CLI (currently done via `linear-gql.sh` `parentId`), and smarter area-label inference from changed files.
