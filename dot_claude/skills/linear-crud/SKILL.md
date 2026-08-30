---
name: linear-crud
description: Manage Linear issues from chat via the `linear` CLI — create, list, move, link, and audit issues in the silverbeer (SB) workspace, with filtering by repo and by epic (Linear project). Use when the user wants to file a ticket, check their open issues, filter issues by epic/project, list epics, see workspace stats / velocity / momentum, change an issue's state, link a PR to an issue, or find unassigned issues. Works in any silverbeer repo on any machine.
allowed-tools: Bash, Read
---

Wraps the already-installed, already-authed `linear` CLI so issue ops are one tool call instead of drafting markdown for the user to paste. Linear is the canonical tracker for all silverbeer repos; the `SB` team holds every project, scoped by a `repo` label group.

The helper lives next to this file: `scripts/linear.sh`. Run it with `bash`. It bakes in the conventions below so you don't have to reconstruct them each time. Cold detail (CLI gotchas, board, metrics, cycle report, estimate history, `driven`, raw recipes, transport) lives in `REFERENCE.md` next to this file.

## Non-negotiable conventions

1. **Every issue is assigned to `silverbeer.io`. Never leave one unassigned.** The helper does this automatically on `new`; for any raw `linear` call you make, pass `--assignee "silverbeer.io"`.
2. **Every issue gets a `repo` label and a `type` label** (both are mutually-exclusive groups). The helper sets these on `new`. A ticket without them is incomplete (SB-74 shipped label-less before this skill existed — don't repeat that).
3. **Confirm before any write.** For `new` and `move`, show the user the exact title / type / repo / epic / area labels / target state you're about to apply and wait for a yes. Reads (`list`, `epics`) run immediately.
4. **Every epic maps to a repo (its required "project-level label").** Epics are Linear *projects*. The epic→repo map is `repos.json` (next to this file); an epic with no mapping is rejected. When you pass `--epic`, the repo label is derived from it — so `--repo` is usually unnecessary. An explicit `--repo` overrides the epic's default with a warning.

### Label vocabulary

- `repo` (pick one, required): see `repos.json` next to this file — one entry per repo label. Add a repo = add an entry (fields in `REFERENCE.md`). Auto-detected from the current git repo; pass `--repo` to override, or `--epic` to derive it from the epic.
- `type` (pick one, required): `bug` · `feature` · `chore` (maintenance/refactor, no behavior change) · `docs` · `infra` (CI/k8s/helm/terraform) · `security`.
- `driven` (pick one, auto-applied): defaults to `driven:human`; an agent promotes its own ticket with `linear.sh driven SB-N agent-supervised` when it opens the PR. Semantics in `REFERENCE.md`.
- area (flat, optional, multi): e.g. `backend`, `frontend`, `db`, `auth`, `qop`, `scraper-integration`. Add with repeated `--label`.

### Epics (Linear projects)

Run `bash scripts/linear.sh epics` to list them with the repo each maps to. Pass the exact name to `--epic`. New epic → add a lowercase glob to its repo's `epicGlobs` in `repos.json` (`REFERENCE.md`).

CLI flag quirks (linear 2.0.0): `REFERENCE.md` → "CLI gotchas".

## Commands

Run from inside the relevant repo so repo-detection works (or pass `--repo`).

### List epics — `/linear epics`
```bash
bash scripts/linear.sh epics    # Linear projects + the repo each maps to
```

### Pack — `/linear pack SB-N`
Everything `/work` needs to start, in one ~400 B JSON. Replaces `view` + `repo-label` +
the branch-name lookup + `git status`:

```bash
bash scripts/linear.sh pack SB-42
# {"issue":{...brief...},"branchName":"silverbeer/sb-42-<slug>","repoLabel":"DOT",
#  "git":{"branch":"main","dirty":false},"pr":{"number":7,"url":"...","state":"OPEN"}}
```

`branchName` is what `branch SB-N` would check out (Linear's own name is not used — it
conflicts with ours). `repoLabel` is `null` outside a mapped repo. `pr` is the most
relevant PR for the branch (OPEN wins over MERGED/CLOSED); `null` if none or no `gh`.

### View — `/linear view SB-N [--full]`
One issue. Default is a one-line brief JSON (~300 B):
`{identifier,title,state,estimate,priority,labels,url}`.

```bash
bash scripts/linear.sh view SB-42          # brief JSON
bash scripts/linear.sh view SB-42 --full   # CLI text incl. description (markdown)
```

Use `--full` only when the AC text itself is needed (SB-905, SB-922).

### Branch — `/linear branch SB-N`
```bash
bash scripts/linear.sh branch SB-42        # checkout silverbeer/sb-42-<slug>
```
See "The delivery loop" below for what the name does and does not trigger.

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

The command prints the new `SB-N` URL — relay it. (`--repo` is optional; omit to auto-detect.)

**Always set an estimate** — `--estimate N`, Fibonacci 1/2/3/5/8; `0` for superseded/duplicate/won't-do. Why, and how to reset one: `REFERENCE.md` → "Estimates".

### List — `/linear list [--all] [--epic E]`
```bash
bash scripts/linear.sh list                          # my open issues in the current repo
bash scripts/linear.sh list --all                    # include done/canceled
bash scripts/linear.sh list --epic "Local Agent Automation"        # open issues in one epic
bash scripts/linear.sh list --epic "Local Agent Automation" --all  # all states in that epic
```
TSV, one per line: `SB-N<TAB>State<TAB>P<prio><TAB>e<est><TAB>title`. `epics` is the
same shape: `name<TAB>status<TAB>[repo]`.

### Move — `/linear move SB-N <state>`
States: `Backlog → Todo → In Progress → In Review → Done` (or `Canceled`). Confirm, then:
```bash
bash scripts/linear.sh move SB-42 "In Progress"
```

### Link — `/linear link SB-N [PR#]`
Adds `Fixes SB-N` to a PR body (idempotent). Defaults to the current branch's open PR:
```bash
bash scripts/linear.sh link SB-42        # current branch's PR
bash scripts/linear.sh link SB-42 420    # explicit PR number
```

### Everything else
`stats`, `board`, `driven`, `audit-unassigned`, `metrics.sh`, `cycle-report.py`, raw CLI
and GraphQL recipes: `REFERENCE.md`.

## The delivery loop (paved road)

State transitions are **automatic** via the GitHub integration — do NOT `move` manually
for these. This is the canonical statement of the rule (verified by experiment SB-938,
2026-08-29); every other doc points here.

Linear keys on the `sb-<n>` token in a **PR head branch name**, any prefix. A branch
push on its own does nothing. A draft PR links the PR to the issue but does not change
state; a non-draft PR (or draft → ready) moves the issue to **In Progress**. Merging a PR
whose body contains `Fixes SB-N` moves it to Done. The `silverbeer/` prefix is a naming
convention only.

```
linear.sh branch SB-N     # checkout silverbeer/sb-n-<slug>  (name carries sb-n)
# …implement + test…
/cppp                     # commit, push, open PR (body: "Fixes SB-N")  → auto: In Progress
# merge the PR            #                                             → auto: Done
```
`linear.sh move` is for grooming (Backlog→Todo), In Review, or exceptions. Branch names
**must** contain `sb-N` — `linear.sh branch` guarantees it; `linear.sh link` guarantees
the `Fixes` line.
