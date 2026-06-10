---
name: linear-crud
description: Manage Linear issues from chat via the `linear` CLI — create, list, move, link, and audit issues in the silverbeer (SB) workspace. Use when the user wants to file a ticket, check their issues, change an issue's state, link a PR to an issue, or find unassigned issues. Works in any silverbeer repo on any machine.
allowed-tools: Bash, Read
---

Wraps the already-installed, already-authed `linear` CLI so issue ops are one tool call instead of drafting markdown for the user to paste. Linear is the canonical tracker for all silverbeer repos; the `SB` team holds every project, scoped by a `repo` label group.

The helper lives next to this file: `scripts/linear.sh`. Run it with `bash`. It bakes in the conventions below so you don't have to reconstruct them each time.

## Non-negotiable conventions

1. **Every issue is assigned to `silverbeer.io`. Never leave one unassigned.** The helper does this automatically on `new`; for any raw `linear` call you make, pass `--assignee "silverbeer.io"`.
2. **Every issue gets a `repo` label and a `type` label** (both are mutually-exclusive groups). The helper sets these on `new`. A ticket without them is incomplete (SB-74 shipped label-less before this skill existed — don't repeat that).
3. **Confirm before any write.** For `new` and `move`, show the user the exact title / type / repo / area labels / target state you're about to apply and wait for a yes. Reads (`list`) run immediately.

### Label vocabulary

- `repo` (pick one, required): `MT` missing-table · `MS` match-scraper · `MSA` match-scraper-agent · `QB` qualityplaybook · `STK` myrunstreak · `TODO` todo (github.com/silverbeer/todo). Auto-detected from the current git repo; pass `--repo` to override. (New repo labels must be created under the `repo` group via `linear api` — see CLI surface below.)
- `type` (pick one, required): `bug` · `feature` · `chore` (maintenance/refactor, no behavior change) · `docs` · `infra` (CI/k8s/helm/terraform) · `security`.
- area (flat, optional, multi): e.g. `backend`, `frontend`, `db`, `auth`, `qop`, `scraper-integration`. Add with repeated `--label`.

### CLI surface (linear CLI v2.0.0, verified 2026-06-10)

The helper bakes this in; match it when you drop to raw `linear`:

- Subcommands are **singular**: `issue` and `label` (e.g. `label list`, **not** `labels list`).
- `issue create`: title via `-t/--title`, labels as **repeated `-l`** (no `--labels` comma list), markdown body via `--description-file` (or `-d`), and `--no-interactive`.
- **Labels are validated against their group**: `repo`/`type` are mutually-exclusive groups — pass exactly one of each. `docs` lives in the `type` group (NOT an area). Two type labels → GraphQL `labelIds not exclusive child labels`.
- **`issue mine`** (aka `list`/`l`) = *your* issues; requires an explicit `--sort manual|priority`. **`issue query`** = all issues with filters: `--assignee <user>`, `-U/--unassigned`, `-l <label>` (repeatable), `-s/--state`, `--all-states` (default), `-j/--json`, `--limit 0` (unlimited).
- State **filter** values are status *types*: `triage|backlog|unstarted|started|completed|canceled` (+ `--all-states`). **Set** state by workflow *name*: `issue update SB-N --state "In Progress"`. ⚠️ `--state` resolves by *type*, so when two states share one type (SB has both **In Progress** and **In Review** as `started`), it silently lands on the wrong one — set those via `api` with the exact `stateId` (`issueUpdate(id, input:{stateId})`).
- `issue update`: `-t/--title`, `-s/--state` (name or type), `-a/--assignee`.
- **`linear api '<graphql>'` works.** Use it for label-group ops — e.g. create a repo label under the `repo` group (the `label create` command has no `--parent`, so set `parentId` via the `issueLabelCreate` mutation). The `repo` group label id is `baaca4e0-c497-4383-8161-3c885abb1e7a`.
- View one issue: `issue view SB-N` (there is no `issue get`). Assignee: pass `silverbeer.io` on create; `me` for `query --assignee me`.

## Commands

Run from inside the relevant repo so repo-detection works (or pass `--repo`).

### Create — `/linear new <free text>`
Infer a concise title, a short markdown description, and the `type` from the conversation. Detect `repo` from cwd. Then **show the user the proposed title + labels and confirm**, then file:

```bash
# write the description to a temp file (markdown-safe), then:
bash scripts/linear.sh new --title "Roster CSV import rejects BOM" \
  --type bug --label frontend --body-file /tmp/issue.md
```
The command prints the new `SB-N` URL — relay it. (`--repo` is optional; omit to auto-detect.)

### List — `/linear list [--all]`
```bash
bash scripts/linear.sh list          # my open issues in the current repo
bash scripts/linear.sh list --all    # include done/canceled
```

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

### Audit unassigned — `/linear audit`
Find (and optionally fix) issues with no assignee — backs convention #1:
```bash
bash scripts/linear.sh audit-unassigned         # list only
bash scripts/linear.sh audit-unassigned --fix   # assign all to silverbeer.io
```

## Anything not covered

Drop to the raw CLI (`linear issue ...`) but keep conventions #1–#3. Useful recipes:
- View one issue: `linear issue view SB-N`
- List/inspect labels (confirm group membership): `linear label list --team SB`
- Add a comment: `linear issue comment add SB-N -b "text"` (or `--body-file /tmp/c.md`)
- Issues by label: `linear issue query --team SB -l TODO --all-states -j`
- Unassigned set: `linear issue query --team SB -U -j` (or use `audit-unassigned`)
- Create a repo label in the `repo` group:
  `linear api 'mutation { issueLabelCreate(input: {name:"X", parentId:"baaca4e0-c497-4383-8161-3c885abb1e7a", color:"#95a2b3"}) { success } }'`

## Phase 2 (not yet built)

Sub-issues via `linear issue relation`, auto branch↔issue linking on `git checkout -b silverbeer/sb-N-...`, smarter area-label inference. Track under SB-18.
