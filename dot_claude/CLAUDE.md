## Where things live

**Skills, agents and this file are managed by chezmoi**, sourced from
`~/gitrepos/dotfiles/dot_claude/`. `~/.claude/` is the *deployed copy* — editing
it changes only this machine.

**Confirm that before trusting it.** chezmoi's default source is
`~/.local/share/chezmoi`, and `chezmoi init` recreates it there. If that clone
still exists it becomes a second, silently divergent copy: edits land in the
git repo, chezmoi keeps applying the other one, and merged PRs never reach the
machine. On the mac mini it sat four commits behind for weeks.

```bash
chezmoi source-path    # MUST print ~/gitrepos/dotfiles
```

If it prints anything else, fix `sourceDir` in `~/.config/chezmoi/chezmoi.toml`
before editing anything. That file is bootstrap config: it cannot manage itself,
so it is per-machine and is documented in SETUP.md rather than tracked here.

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

## 1Password: which vault, and what the errors mean

Agent shells (`CLAUDECODE=1`, and any non-interactive zsh) authenticate `op`
with a **service-account token**, exported from `~/.zshenv`, read from
`~/.config/op/agent-token`. Fully headless — no biometric, no "access data from
other apps" prompt. Interactive terminals are unchanged and still use the
desktop app.

**`agents` is the only vault an agent can see.** Use `op://agents/<item>/<field>`.

**An `op://Personal/...` failure means wrong vault. It is not a lock, and
retrying will never fix it.** A 1Password service account can never be granted
the built-in Personal/Private vault — that is a product constraint, not a
permission that someone forgot to add. Do not retry, do not try to unlock, do
not ask the user to touch the fingerprint reader. Rewrite the reference or, if
the item genuinely only exists in Personal, stop and say so.

**Access is read-only.** `op item create`, `edit` and `move` all fail. Work that
needs a write has to happen in a human terminal — stop and ask.

**The token expires 2026-11-19.** An auth failure on or near that date is expiry,
not a bug.

### Telling the three failures apart

They are indistinguishable from the error text alone, so check state instead:

```bash
[ -r ~/.config/op/agent-token ] || echo "NO TOKEN — machine not provisioned"
op whoami        # 'User Type: SERVICE_ACCOUNT' = token present and valid
op vault list    # exactly one row, 'agents', is the healthy state
```

- **Absent** — `~/.config/op/agent-token` missing. This machine was never
  provisioned; the token cannot bootstrap itself from 1Password. See SETUP.md
  "Step 2b" and ask the user to run it. Nothing you can do from an agent shell.
- **Wrong vault** — `op whoami` fine, `op vault list` shows `agents`, but the
  read fails. The reference is pointing at Personal. Fix the reference.
- **Expired** — `op whoami` itself fails on a token file that exists. Issue a
  replacement service-account token and rewrite the file.

### Things that are still not in the vault

- `mt-android-release` remains in `Personal`. `scripts/set-release-secrets.sh`
  and the android `set-release-secrets.sh` read it and honour `OP_VAULT` /
  `OP_ITEM` — leave their defaults alone.
- `Supabase MSA` remains in `Personal`, which is why `SUPABASE_PG_URL` is
  provisioned to `~/.config/supabase/pg-url` from a human terminal rather than
  read at apply time.
- `mt` prod login still reads `TEST_USER_PASSWORD_TOM` from `backend/.env.prod`,
  **not** 1Password. `mt login` stops at the first source it finds, so the vault
  path is inert for `mt-prod` until SB-840 removes those four keys. Changing the
  vault item will appear to do nothing.

### Never print a secret

Agent output goes to a permanent transcript. Verify by exit code, byte count or
a hash prefix — never by value, and never with `cat` on a file that might hold
one. `${VAR:-x}` expands to the *value* when VAR is set; to test whether a
variable is set, use `[ -n "$VAR" ] && echo set`. Both of those mistakes have
already leaked a live credential here.

## The delivery loop

Linear (team **SB**) is the system of record. Tools in
`~/.claude/skills/linear-crud/scripts/`.

```bash
bash ~/.claude/skills/linear-crud/scripts/linear.sh branch SB-123
```

Branch name carries `sb-123`, and the PR body carries `Fixes SB-123`; opening a
non-draft PR and merging it drive the ticket's state (details: linear-crud
SKILL.md, "The delivery loop"). **Merge your own green PRs; don't wait to be asked.**

The integration is one-way: GitHub events drive Linear, never the reverse.
Changing a status in Linear does nothing in GitHub.

### Clean up the moment a PR merges

Disk is a managed resource here — the mac mini runs agents, k3s and many
worktrees at once. A merge is not done until its leftovers are gone. Three
layers, each catching what the last missed:

1. **GitHub** deletes the remote branch: always `gh pr merge --delete-branch`.
2. **Whoever merged** removes their own worktree and local branch, right then.
3. **`jt git prune`** (janitor) sweeps every repo under `~/gitrepos` nightly at
   03:30 via launchd. Rules and diagrams: `~/gitrepos/janitor/docs/git-hygiene.md`.

```bash
gh pr merge <N> --squash --delete-branch          # 1
git worktree remove <path>                        # 2 — if the work was in a worktree
git branch -D <branch>                            # 2 — squash merges need -D
linear.sh worktree-done SB-N                      # 2 — same, for a linear.sh worktree
```

**When to run which `jt` command** — these are triggers, not suggestions:

| Moment | Run |
|---|---|
| You merged your own PR | step 2 above, directly. Then `jt git audit --repo .` to confirm nothing of yours is left |
| Before `/handoff` or ending a session that made branches/worktrees | `jt git audit --repo .`; remove your own leftovers; mention REVIEW items |
| User asks to clean up branches / worktrees / disk | `jt --dry-run git prune`, show the summary, then `jt --yes git prune` |
| `jt doctor` shows **Git hygiene ⚠**, or disk is low | `jt git schedule status`, then `jt git audit` and `jt --yes git prune` |
| New machine, or `jt git schedule status` says not loaded | `jt git schedule install` |
| `jt doctor` shows **k3s build cache ⚠** or **k3s upkeep ⚠** | `jt k3s schedule status`, then `jt --yes k3s maintain` (prunes dangling images + BuildKit cache, trims the VM) |
| New machine with Rancher Desktop, or `jt k3s schedule status` says not loaded | `jt k3s schedule install` (weekly, Sunday 04:00) |

- `jt git prune` only deletes **PRUNE** items (PR merged on GitHub, nothing
  committed after; worktree unlocked and clean except build artefacts). It is
  safe to run without asking. **REVIEW** items — closed-unmerged PRs, commits
  after merge, uncommitted changes — are never yours to delete: list them for
  the user.
- Removing a worktree by hand: `git status --porcelain` must be empty apart
  from build artefacts (`.venv`, `node_modules`, caches). Anything else is
  unsaved work — stop and say so; never `--force` past it.
- By hand, only remove worktrees **you** created this session. Leave `locked`
  ones and anything a running process may own; `jt` handles the rest by rule.

## Unplanned work is the norm

Every project here is **pre-user**. Requests arrive daily and take precedence
over the groomed backlog — that's the operating condition, not scope creep.

Tag unplanned tickets `adhoc`. They skip grooming ceremony entirely: title, repo
label, `adhoc`, then branch and build. Priority and epic get filled at the next
groom, or never.

**Always set an estimate when filing a ticket** (Fibonacci: 1/2/3/5/8). A first
guess is fine — revise it at close if reality differed; the drift is itself
signal. Use `0` for anything closed as superseded, duplicate or won't-do, so
velocity doesn't count work nobody did. Unestimated tickets silently halve the
measured throughput.

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

The `todo` skill is the user's **life** list — a second brain for errands,
admin, people, health. It is not a project tracker and does not sync between
machines. Never offer it for anything about a repo, a ticket, an environment
or a setup step: those are project work and belong in Linear — file an
`adhoc` ticket, or add a checklist to the ticket they block. The agentic loop
only sees Linear; a project task in `todo` is invisible to it.

When a session surfaces a genuinely personal follow-up (call someone, pay
something, buy something), briefly offer to capture it with the `todo` skill —
one line, ask before adding. Don't nag; skip if the user is clearly busy or has
declined recently.

## Look it up before asserting it

Do not state regulations, broker or vendor capabilities, product availability, or
API surfaces from training data. Search first, then say it, and cite the source.

Anything that would change **what gets built or funded** and depends on the
outside world moving — a rule, a regulator, a broker, a pricing page, whether some
product has an API — is exactly where the training cutoff lies most convincingly.
The failure is silent: a confident, specific, plausible answer that is simply out
of date.

Worked example, 2026-08-01: three blockers were given against a live trading
engine, and two were stale facts stated as certainties — that FINRA's pattern day
trader rule capped day trades under $25k (eliminated 2026-06-04), and that
Robinhood had no official API so any integration would be reverse-engineered and
against ToS (they shipped an official agentic-trading MCP in June 2026). Both were
corrected by the user with sources. The third blocker held, and the reason it held
is the general lesson:

**Arguments from the user's own measured data survive; arguments from recalled
external facts decay.** When both are available, lead with the measurement.

@RTK.md
