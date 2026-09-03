# silverbeer dotfiles

Personal Claude Code configuration, shell setup, and development standards managed with [chezmoi](https://www.chezmoi.io/) + [1Password CLI](https://developer.1password.com/docs/cli/).

> **Platform:** macOS only. Requires [Claude Code](https://claude.ai/code) installed and licensed.
>
> **Note:** This is a personal dotfiles repo. The `zshrc` references personal paths and 1Password items you won't have. Use it as reference — adapt to your own stack. The Claude agents, commands, skills, RTK setup, and chezmoi patterns are fully portable.

---

## What this repo is

If you're new to Claude Code, this repo is a working example of how to get serious about it. It covers:

- **RTK** — a hook that cuts token usage 60-90% on every CLI command Claude runs
- **Subagents** — specialized AI personas (e.g. a QE engineer) that Claude delegates to automatically
- **Slash commands** — reusable workflows you invoke with `/command`
- **Secrets management** — no plaintext tokens, ever, using 1Password
- **Multi-machine sync** — one command to bootstrap a new Mac identically

If you're evaluating whether to invest in this kind of setup: the answer is yes. The productivity gains compound fast, and the pattern scales from solo developer to a full engineering team.

---

## Quick start (new machine)

```bash
# 1. Install tools
brew install chezmoi gh rtk
brew install --cask 1password-cli

# 2. Enable 1Password CLI desktop integration
# 1Password app → Settings (⌘,) → Developer → "Integrate with 1Password CLI"

# 3. Authenticate GitHub
gh auth login

# 4. Apply dotfiles (clones this repo, renders templates, writes all config)
chezmoi init --apply https://github.com/silverbeer/dotfiles

# 5. Reload shell
source ~/.zshrc

# 6. Verify RTK hook is active
rtk init --show && rtk gain
```

---

## What's managed

| File | Purpose |
|------|---------|
| `~/.zshrc` | Shell config — secrets pulled from 1Password at apply time |
| `~/.claude/CLAUDE.md` | Global Claude instructions (branch protection, GitOps standards) |
| `~/.claude/RTK.md` | RTK config — owned by RTK, do not edit manually |
| `~/.claude/settings.json` | Hooks (RTK rewrite), statusline, global permissions |
| `~/.claude/agents/` | Global Claude subagents (available in every project) |
| `~/.claude/commands/` | Global slash commands (available in every project) |
| `~/.claude/skills/` | Global skills — **allowlisted one-by-one in `.chezmoiignore`** (see below) |

Not everything here is deployed. `k3s/` holds cluster manifests and the
cycle-runner's `Dockerfile` — repo-only, like `README.md` and `.github/`.
It is not dot-prefixed, so the `k3s` line in `.chezmoiignore` is the *only*
thing keeping a `~/k3s` off every machine; `check-no-ci-config-in-home.sh`
fails the build if it ever becomes a target.

### Adding a new skill (the `.chezmoiignore` gotcha)

`~/.claude/skills/` is full of runtime junk and third-party installs, so `.chezmoiignore`
ignores `.claude/skills/*` wholesale and allowlists each synced skill with a `!` exception:

```gitignore
.claude/skills/*
!.claude/skills/linear-crud
!.claude/skills/todo
!.claude/skills/session-audit
```

**Without an exception line, `chezmoi add` silently ignores the skill** — no error, no
warning beyond a one-line notice, and the skill never reaches other machines.

Flow for a new skill:

1. Create it in `~/.claude/skills/<name>/`
2. Add `!.claude/skills/<name>` to `.chezmoiignore`
3. `chezmoi add ~/.claude/skills/<name>/<files...>`
4. Branch, commit, PR (this repo uses the PR workflow)
5. Other machines: merge, then `chezmoi update`

Commands and agents need no allowlist step — `~/.claude/commands/` and `~/.claude/agents/`
are not ignored, so plain `chezmoi add` works.

Step 2 is the one that gets forgotten, so CI fails the PR if you skip it.

---

## CI

`.github/workflows/ci.yml` runs on every PR to `main` (and on pushes to `main`).
Five jobs in parallel, then a single `all green` gate:

| Job | What it protects |
| --- | --- |
| `shellcheck` | Every tracked `*.sh`, plus the two scripts the glob misses: `modify_settings.json.tmpl` (bash, despite the name) and `run_onchange_install-brew-tools.sh.tmpl` (de-templated first). Config: inline `# shellcheck disable=` with a reason. |
| `ruff` | Every tracked `*.py`. Config: `.ruff.toml`, which pins `required-version` so a drifted ruff fails loudly instead of linting with a different rule set. Also runs `check-linear-crud.sh`: offline functional tests for the linear-crud and backlog-groom scripts (repo/epic label table, `stats` jq, `pack` schema, board graph helpers, `apply.py` idempotence) against stubs — no key, no network. Also runs `check-gatekeeper.sh`: offline unit tests for the gatekeeper skill's Telegram transport and dual-channel (Linear + Telegram) gate logic against a fake `gql` — no network, no subprocess, no real token. Also runs `check-cycle-runner-policy.sh`: offline tests for the cycle-runner skill's `pick.py` (driven x estimate x label autonomy policy, ready-queue blocker detection) and `run.sh` (lock acquire/contend/stale-recovery, CI-state reduction, merge-gate decision, sourced with a stubbed `gh`) — no network, no real `claude`/`gh`/Telegram call. Also runs `check-doctor.sh`: offline tests for `doctor.sh`'s cycle-runner + chezmoi-sync checks (OAuth-token validity, `gh auth status`, Telegram `getMe`, stale-lock detection, `chezmoi status` clean-before-re-add) run for real against a scratch `$HOME` with `op`/`gh`/`chezmoi`/`claude` stubbed — no network, no real 1Password read, `claude -p`, or Telegram call. |
| `chezmoi (ubuntu / macos)` | Templates render, the `modify_` script merges correctly and idempotently, `apply --dry-run` succeeds into a scratch `$HOME`, every skill really is managed, and no CI config leaks into `$HOME`. Also runs `check-launchd-plist.sh`: the **triage** launchd agent template renders a valid plist only when `.chezmoi.hostname` is the mac mini's, and renders to nothing — no file at all — everywhere else, verified both by rendering and by a full `apply --dry-run`. (The cycle-runner's own plist is gone — it is a k3s CronJob now, SB-976/SB-979.) |
| `gitleaks` | Full history and working tree. Config: `.gitleaks.toml` — allowlists are by regex (`op://` references, the agent-token path), never by path, so a real key pasted into `SETUP.md` is still caught. |
| `check tests` | Runs `.github/tests/` — the test suite for the checks themselves. |

Two things to know before editing the workflow:

- **Every `chezmoi apply` in CI must keep `--dry-run`.** Without it, the `run_*`
  scripts execute — `op read` against a vault the runner cannot see, and
  `brew install`. `check-no-bare-apply.sh` enforces this statically, because the
  dry-run checks themselves cannot: with the flag gone they would pass while
  doing real work.
- `.ruff.toml` and `.gitleaks.toml` are dot-prefixed on purpose. A root-level
  file that is not dot-prefixed becomes a chezmoi target and gets written into
  `$HOME`; `check-no-ci-config-in-home.sh` asserts that has not happened. Note
  that the `.chezmoiignore` entries for these are NOT a backstop — they list the
  *dotted* names, so a rename to `ruff.toml` sails straight past them.

### Where the check logic lives

In `.github/scripts/check-*.sh`, **not** in the workflow's `run:` blocks. Each
job step is a one-line call to a script.

That is what makes the checks testable. `.github/tests/` runs those same
scripts, so there is exactly one copy of every check — a check inlined in
`ci.yml` and duplicated in a test would be verified in one place and executed in
another, which is worse than having no test at all. `test_meta.sh` fails the
build if check logic reappears in `ci.yml`.

### Running the checks and their tests locally

```bash
.github/tests/run.sh              # the whole suite
.github/tests/run.sh gitleaks     # just files matching *gitleaks*
.github/scripts/check-ruff.sh     # one check, on its own
```

No network, no installs, and nothing is written outside a `mktemp -d` — every
mutating test works on a throwaway copy of the source tree, and every `chezmoi`
call runs with `HOME` pointed at a scratch directory. Checks whose binary is
missing report `SKIP` with a reason rather than failing, and the summary counts
skips separately from passes, so a machine that is quietly testing nothing is
visible. In CI `QE_NO_SKIPS=1` turns any skip into a failure.

Every check is tested in both directions. A check that can only pass is
decoration, so for each one there is at least one test that breaks the input in
a scratch copy and asserts the check *fails*, and asserts the error message is
the right one — a typo in a fixture also produces a non-zero exit.

Pinned versions, if you want to reproduce a job exactly: shellcheck 0.11.0,
ruff 0.16.5, gitleaks 8.30.0, chezmoi v2.70.0.

---

## RTK — The Single Highest-Impact Tool Here

[RTK (Rust Token Killer)](https://github.com/rtk-ai/rtk) is a Claude Code `PreToolUse` hook that transparently intercepts every Bash command and strips unnecessary output before it reaches Claude's context window.

**Real numbers from this setup: 1.7M tokens saved across 2,843 commands — 39% reduction.**

### Why it matters

Every time Claude runs a CLI command, the raw output comes back full of decorators, progress bars, padding, and noise that Claude doesn't need. RTK filters it out automatically — Claude gets clean, structured output and you burn far fewer tokens.

```
git status   →   rtk git status    (clean diff, no decorators)
ls           →   rtk ls            (72% savings — biggest single win)
gh pr create →   rtk gh pr create  (filtered output)
kubectl get  →   rtk kubectl get   (trimmed pod tables)
pytest       →   rtk pytest        (structured test results only)
```

The rewrite is invisible. Claude asks to run `git status`, RTK silently rewrites it to `rtk git status`, Claude gets clean output. Zero behavior change.

**If you let Claude Code handle your git workflow — and you should — RTK is non-negotiable.**

### Install and wire up

```bash
brew install rtk
rtk init -g       # Install hook + RTK.md into ~/.claude
# Restart Claude Code
```

This dotfiles repo pre-configures the hook in `~/.claude/settings.json`. After `chezmoi init --apply`, just verify:

```bash
rtk init --show   # Hook is registered
rtk gain          # Cumulative savings summary
rtk discover      # Find commands that slipped past RTK coverage
```

---

## Claude Code Primitives — Know the Difference

Claude Code has three ways to extend its behavior. Understanding the distinction is essential before building your own.

| Type | How to invoke | Model control | Isolated context | Lives in |
|------|--------------|--------------|-----------------|----------|
| **Slash command** | `/cppp`, `/qe` | No — uses session model | No — shares session | `~/.claude/commands/*.md` |
| **Subagent** | `@qe-engineer` | Yes — `model:` in frontmatter | Yes — own context window | `~/.claude/agents/*.md` |
| **Skill** | `/load-tournament-matches` | No — uses session model | No — shares session | `~/.claude/skills/*/SKILL.md` |

### What this means in practice

**Slash commands** are the simplest. They're markdown files containing a prompt. When you type `/cppp`, Claude reads the file and executes it in your current session. No overhead, no model selection, instant.

**Subagents** are specialized AI personas. Claude can delegate to them mid-conversation. They run with their own context window and their own model — you choose which. Use `sonnet` for agents that need to understand code and business logic. Use `haiku` for mechanical agents doing formatting or summarizing.

**Skills** work like slash commands but are packaged differently and can include more structured metadata. Functionally similar for most purposes.

### Cost strategy: home vs. team

| Context | Recommendation |
|---------|---------------|
| **Personal ($100/month flat)** | RTK to avoid quota. Model selection on subagents matters less — use Sonnet everywhere for best quality. |
| **Team (pay-per-token)** | RTK is mandatory — 39% savings for free. Audit every subagent model choice. Mechanical subagents (commit formatting, report generation) → Haiku. Code-understanding subagents → Sonnet. |

The key insight: **slash commands have no model control** — they run in whatever model the session is using. If you want a cheaper model for a specific workflow, it needs to be a subagent, not a slash command.

---

## The Layering Model

Global config applies everywhere. Project config overrides globally. Same filename = project wins.

```
~/.claude/agents/qe-engineer.md     ← universal QE rules (this repo)
~/.claude/commands/cppp.md          ← universal commit/PR workflow (this repo)
        ↓ project overrides by same filename
.claude/agents/qe-engineer.md       ← project test runner, mocking patterns, open issues
.claude/commands/cppp.md            ← project deployment pipeline awareness
```

**Rule:** Global = universal rules that never change. Project = only what's unique to that repo. Never duplicate content between layers.

---

## Global Agents

### `@qe-engineer` — Quality Engineering

A senior QE engineer embedded in every project. Writes tests, enforces coverage, and won't let gaps slide.

**Model:** `sonnet` — needs to understand business logic to write meaningful tests. Haiku would write syntactically correct tests that miss the point.

**How to use:**
```
@qe-engineer write tests for src/audit/processor.py
@qe-engineer review what I just changed and write missing tests
/qe   ← first — run a coverage audit to find the gaps
```

**What it does:**
- Identifies all untested or under-tested functions in any module
- Writes pytest tests following the project's existing conventions
- Runs the suite and fixes failures before returning
- Reports what's covered and what's still open

**Project overrides add:** test runner command, stack-specific mocking patterns, open GitHub issues tracking test debt.

---

## Global Slash Commands

### `/cppp` — Commit, Push, PR

Standard commit and PR workflow enforcing conventional commits, branch hygiene, co-authorship, and safe staging.

**Usage:** Type `/cppp` when you're ready to commit and open a PR.

**What it does:**
- Checks `git status` and `git diff` before staging anything
- Enforces conventional commit format (`feat:`, `fix:`, `chore:`, `test:`, etc.)
- Creates PR with summary + test plan body via `gh pr create`
- Adds `Co-Authored-By: Claude Sonnet 4.6` to every commit
- Never commits to `main` directly, never skips hooks

**Project overrides add:** deployment pipeline awareness so Claude knows what happens after merge (ArgoCD, K3s, etc.) and never tries to manually trigger builds.

---

### `/qe` — Coverage Audit

Runs a test coverage report and produces a prioritized action list.

**Usage:** Type `/qe` for a coverage report on the current project.

**Output:**
- Pass/fail count
- Module-by-module coverage table: `CRITICAL / HIGH / MEDIUM / OK`
- Cross-reference with open GitHub issues already tracking gaps
- Ends with the exact command to fix the top gap: `@qe-engineer write tests for <module>`

---

## Per-Project Overrides

### missing-table (MT) — Full-stack FastAPI + Vue.js

| File | Purpose |
|------|---------|
| `.claude/skills/cppp/SKILL.md` | ArgoCD + kubectl deployment awareness |
| `.claude/agents/qe-engineer.md` | pytest-asyncio, DAO mocking, Supabase fixtures, test structure |
| `.claude/settings.local.json` | 200+ allowed commands for full-stack dev |

**Deployment:** PR merge → GHA builds Docker → updates `helm/values-prod.yaml` → ArgoCD syncs (~5 min)

### match-scraper-agent (MSA) — Python pipeline, K3s

| File | Purpose |
|------|---------|
| `.claude/commands/cppp.md` | K3s CronJob deployment awareness |
| `.claude/agents/qe-engineer.md` | uv/pytest, audit module patterns, open test debt in #49–52 |

**Deployment:** PR merge → GHA builds `linux/amd64` Docker → updates `k3s/cronjob.yaml` → K3s pulls new image

---

## Adding This Pattern to a New Repo

```bash
# 1. Create the .claude structure
mkdir -p .claude/agents .claude/commands

# 2. QE agent override
cat > .claude/agents/qe-engineer.md << 'EOF'
---
name: qe-engineer
description: QE engineer for <project>. Writes tests and enforces coverage.
tools: Bash, Read, Edit, Write, Grep, Glob
model: sonnet
---

Follows the global qe-engineer rules. Project-specific additions:

## Test runner
<how to run tests in this project>

## Stack-specific patterns
<mocking patterns, fixtures, etc.>

## Open test debt
<link to GitHub issues>
EOF

# 3. cppp deployment override
cat > .claude/commands/cppp.md << 'EOF'
Follows the standard cppp workflow. Project-specific deployment pipeline:

## Deployment
<what happens after PR merge>

## Verify deployment
<commands to check status>
EOF

# 4. Commit — everyone who clones the repo gets these instantly
git add .claude/ && git commit -m "chore: Add Claude Code agents and commands"
```

---

## Applying This to a Professional Team

The same pattern works at team scale. The key insight: **anything committed to `.claude/` in a repo is automatically shared with every developer who clones it.** Project-level agents and commands are free team productivity wins with zero per-developer setup.

| Personal setup | Team adaptation |
|----------------|----------------|
| `~/.claude/agents/` in dotfiles | Team dotfiles repo or onboarding runbook |
| Secrets in personal 1Password | Team 1Password vault or CI secret manager |
| Project `.claude/agents/` in repos | Same — commit to repo, whole team benefits immediately |
| RTK on personal machine | Mandate in team onboarding — immediate cost reduction |
| Subagents on Sonnet (flat rate) | Audit subagent models — mechanical work → Haiku saves real money |

**Recommended team rollout order:**
1. RTK for everyone — biggest immediate ROI, zero workflow change
2. `/cppp` in every repo — consistent commits and PRs across the team
3. `@qe-engineer` per repo — test coverage improves passively as features ship
4. Team dotfiles repo — standardizes the baseline across all developers

---

## What is 1Password CLI — and is it required?

[1Password CLI](https://developer.1password.com/docs/cli/) (`op`) lets chezmoi templates pull secrets from your 1Password vault at apply time, so API keys and tokens never get committed to git.

**Is it required?** No — but without it, any `.tmpl` files that reference `onepasswordRead` will fail when you run `chezmoi apply`. This repo's `dot_zshrc.tmpl` uses it.

**If you don't use 1Password**, you have two options:
- Replace the `{{ onepasswordRead "..." }}` calls in `.tmpl` files with literal values (but don't commit secrets)
- Use chezmoi's built-in support for other secret managers — it supports [Bitwarden, LastPass, Keychain, plain env vars, and more](https://www.chezmoi.io/user-guide/password-managers/)

**If you do use 1Password**, enable the desktop integration so the CLI can auth without a separate login:
> 1Password app → Settings (⌘,) → Developer → "Integrate with 1Password CLI"

---

## What is chezmoi?

[chezmoi](https://www.chezmoi.io/) is a dotfiles manager. "Dotfiles" are the config files that live in your home directory — things like `~/.zshrc`, `~/.claude/settings.json`, etc. — and keeping them in sync across multiple Macs is normally a pain.

chezmoi solves this by:

1. **Storing your dotfiles in a git repo** — so they're versioned and shareable
2. **Applying them to the right places** — `chezmoi apply` writes the files into your home directory
3. **Supporting templates** — files ending in `.tmpl` can reference secrets or machine-specific values, so your actual `~/.zshrc` gets generated from a template that pulls passwords from 1Password rather than hardcoding them

**Why chezmoi instead of a plain symlink approach?**

Plain symlink setups (symlinking `~/dotfiles/.zshrc` → `~/.zshrc`) break the moment a file needs to be different per machine or contain a secret. chezmoi handles both — the source stays clean in git, the rendered output lands in the right place with secrets injected at apply time.

**The tradeoff:** chezmoi hides its source in `~/.local/share/chezmoi`, which isn't obvious. This repo includes a symlink at `~/dotfiles` so you can find it easily.

---

## Day-to-day Chezmoi Workflow

```bash
chezmoi edit ~/.zshrc        # Edit a managed file
chezmoi diff                 # Preview changes before applying
chezmoi apply                # Apply to home directory
chezmoi update               # Pull latest from GitHub and apply (other machine)

# Commit and push changes
cd ~/.local/share/chezmoi
git add -A && git commit -m "chore: describe change" && git push
```

## Managing Secrets

```bash
# Add a new secret to 1Password
op item create --category="API Credential" --title="My Service" \
  --vault="Personal" "credential=abc123"

# Reference it in any .tmpl file
{{ onepasswordRead "op://Personal/My Service/credential" }}

chezmoi apply   # Renders template, injects secret at apply time
```
