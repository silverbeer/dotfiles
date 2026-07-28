## Where things live

**Skills, agents and this file are managed by chezmoi**, sourced from
`~/gitrepos/dotfiles/dot_claude/`. `~/.claude/` is the *deployed copy* — editing
it changes only this machine.

After changing anything under `~/.claude/`:

```bash
chezmoi re-add ~/.claude/<path>   # existing file
chezmoi status                    # MM = diverged from source, still unsynced
```

**Adding a new skill needs one extra step first.** `.chezmoiignore` ignores
`.claude/skills/*` wholesale, so a new skill needs an explicit
`!.claude/skills/<name>` exception added *before* `chezmoi add` — otherwise the
add reports success and syncs nothing.

Changes land via a PR to `silverbeer/dotfiles`. Other machines pick them up with
`chezmoi update`. **Work that isn't written back exists on one machine only.**

Memory lives in `~/.claude/projects/<slug>/memory/` and is **not** currently
synced between machines.

## The delivery loop

Linear (team **SB**) is the system of record. Tools in
`~/.claude/skills/linear-crud/scripts/`.

```bash
bash ~/.claude/skills/linear-crud/scripts/linear.sh branch SB-123
```

Creates `silverbeer/sb-123-<slug>` and auto-moves the ticket to In Progress on
push. Put `Fixes SB-123` in the PR body — merging auto-transitions it to Done.
**Merge your own green PRs; don't wait to be asked.**

The integration is one-way: GitHub events drive Linear, never the reverse.
Changing a status in Linear does nothing in GitHub.

## Unplanned work is the norm

Every project here is **pre-user**. Requests arrive daily and take precedence
over the groomed backlog — that's the operating condition, not scope creep.

Tag unplanned tickets `adhoc`. They skip grooming ceremony entirely: title, repo
label, `adhoc`, then branch and build. Priority and epic get filled at the next
groom, or never.

`python3 ~/.claude/skills/linear-crud/scripts/cycle-report.py` shows the
planned-vs-adhoc split. Plan each cycle at roughly `100% − adhoc share` of
capacity.

## Branch Protection Standards

All repositories should have branch protection enabled on the `main` branch:

### Required Settings
- **Require a pull request before merging**: Yes
- **Require approvals**: No (single-user repos - creator must be able to merge their own PRs)
- **Require status checks to pass before merging**: Yes (when CI exists)

### GitOps CI Access
- CI workflows that commit to main (e.g. image tag updates) must use a Fine-grained PAT (`GH_PAT` secret) with `contents: write` permission
- PAT owner permissions bypass branch protection
- Always include `[skip ci]` in automated commits to prevent loops

### Why This Matters
- Audit trail: All changes go through PRs
- Habit building: Even solo developers benefit from PR workflow
- GitOps compatibility: Allows automated tag updates while maintaining protection

## Personal Todo Capture

When a session surfaces a concrete follow-up or deferred task that lives beyond
the current work (not an in-PR TODO), briefly offer to capture it with the
`todo` skill — one line, ask before adding. Don't nag; skip if the user is
clearly busy or has declined recently.

@RTK.md
