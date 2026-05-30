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

- `repo` (pick one, required): `MT` missing-table · `MS` match-scraper · `MSA` match-scraper-agent · `QB` qualityplaybook · `STK` myrunstreak. Auto-detected from the current git repo; pass `--repo` to override.
- `type` (pick one, required): `bug` · `feature` · `chore` (maintenance/refactor, no behavior change) · `docs` · `infra` (CI/k8s/helm/terraform) · `security`.
- area (flat, optional, multi): e.g. `backend`, `frontend`, `db`, `auth`, `qop`, `scraper-integration`. Add with repeated `--label`.

### CLI gotchas (learned the hard way — the helper avoids them)

- `--no-interactive` is valid on `issue create` but **errors on `issue update`**.
- The `self` keyword is unreliable for `--assignee`; always use the username string `"silverbeer.io"`.
- `linear issue list` needs a sort flag; prefer `linear issue mine` (what the helper uses).

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

Drop to the raw CLI (`linear issue ...`, `linear api '{...}'`) but keep conventions #1–#3. Useful raw recipes:
- View one issue: `linear issue view SB-N`
- Add a comment from a file: `linear issue comment add SB-N --body-file /tmp/c.md`
- Raw unassigned query: `linear api '{ issues(filter:{team:{key:{eq:"SB"}},assignee:{null:true}}){nodes{identifier}} }'`

## Phase 2 (not yet built)

Sub-issues via `linear issue relation`, auto branch↔issue linking on `git checkout -b silverbeer/sb-N-...`, smarter area-label inference. Track under SB-18.
