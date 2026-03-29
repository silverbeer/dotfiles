# silverbeer dotfiles

Personal Claude Code configuration, shell setup, and development standards managed with [chezmoi](https://www.chezmoi.io/) + [1Password CLI](https://developer.1password.com/docs/cli/).

## Quick start (new machine)

```bash
# 1. Install tools
brew install chezmoi gh
brew install --cask 1password-cli

# 2. Enable 1Password CLI desktop integration
# 1Password app → Settings (⌘,) → Developer → Enable "Integrate with 1Password CLI"

# 3. Authenticate GitHub
gh auth login

# 4. Apply everything
chezmoi init --apply https://github.com/silverbeer/dotfiles

# 5. Reload shell
source ~/.zshrc
```

---

## What's managed

| File | Purpose |
|------|---------|
| `~/.zshrc` | Shell config — secrets via 1Password |
| `~/.claude/CLAUDE.md` | Global Claude instructions (branch protection, GitOps standards) |
| `~/.claude/RTK.md` | RTK token-saving proxy config |
| `~/.claude/settings.json` | Hooks, statusline, global permissions |
| `~/.claude/mcp.json` | MCP server config (Gmail) |
| `~/.claude/agents/` | Global Claude subagents |
| `~/.claude/commands/` | Global slash commands |

---

## Claude Code Agent & Command System

This is the pattern for managing Claude Code intelligence across all personal and professional projects.

### The layering model

```
~/.claude/agents/          ← GLOBAL baseline (this repo, all projects)
~/.claude/commands/        ← GLOBAL slash commands (this repo, all projects)
        ↓ overridden by
.claude/agents/            ← PROJECT-SPECIFIC (in each repo, same filename wins)
.claude/commands/          ← PROJECT-SPECIFIC slash commands
```

**Rule:** Global files contain universal rules. Project files contain only what's unique to that project. Never duplicate content between layers.

---

## Global Agents

### `qe-engineer` — Quality Engineering

**File:** `~/.claude/agents/qe-engineer.md`

A senior QE engineer embedded in every project. Writes tests, enforces coverage, and won't let gaps slide.

**Trigger phrases:**
- `@qe-engineer write tests for src/audit/processor.py`
- `@qe-engineer review what I just changed and write missing tests`
- `/qe` — runs a full coverage audit report

**What it does:**
- Identifies untested functions in any module you point it at
- Writes pytest tests following the project's existing conventions
- Runs the suite and fixes failures before returning
- Reports what's covered and what's still open

**Project overrides add:**
- Project-specific test runner command
- Stack-specific mocking patterns
- Open GitHub issues tracking test debt

---

## Global Commands (Slash Commands)

### `/cppp` — Commit, Push, PR

**File:** `~/.claude/commands/cppp.md`

Standard commit and PR workflow. Enforces conventional commits, branch hygiene, co-authorship, and safe staging practices.

**Usage:** Type `/cppp` when you're ready to commit and open a PR.

**What it does:**
- Checks git status and diff before staging
- Enforces conventional commit format (`feat:`, `fix:`, `chore:`, etc.)
- Creates PR with summary + test plan body via `gh pr create`
- Adds `Co-Authored-By: Claude Sonnet 4.6` to every commit
- Never touches `main` directly, never skips hooks

**Project overrides add:** Deployment pipeline awareness (ArgoCD, K3s, etc.)

---

### `/qe` — Coverage Audit

**File:** `~/.claude/commands/qe.md`

Runs a test coverage audit and produces a prioritized action list.

**Usage:** Type `/qe` for a coverage report on the current project.

**Output:**
- Pass/fail count
- Module-by-module coverage table (CRITICAL / HIGH / MEDIUM / OK)
- Cross-reference with open GitHub issues
- Ends with: "To fix the top gap now, say: @qe-engineer write tests for \<module\>"

---

## Per-Project Overrides

Each active repo has a `.claude/` directory with project-specific configuration.

### missing-table (MT)

| File | Purpose |
|------|---------|
| `.claude/skills/cppp/SKILL.md` | ArgoCD + K8s deployment pipeline awareness |
| `.claude/agents/qe-engineer.md` | MT test conventions (pytest-asyncio, Supabase fixtures) |
| `.claude/settings.local.json` | 200+ allowed commands for full-stack dev |

**Deployment:** PR merge → GHA builds Docker → updates `helm/values-prod.yaml` → ArgoCD syncs (~5 min total)

### match-scraper-agent (MSA)

| File | Purpose |
|------|---------|
| `.claude/commands/cppp.md` | K3s CronJob deployment pipeline awareness |
| `.claude/agents/qe-engineer.md` | MSA test conventions (uv/pytest, audit module debt tracked in #49–52) |

**Deployment:** PR merge → GHA builds `linux/amd64` Docker → updates `k3s/cronjob.yaml` → K3s pulls new image

---

## Adding this pattern to a new repo

1. **Copy the QE agent override template:**
   ```bash
   mkdir -p .claude/agents
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
   ```

2. **Copy the cppp command override template:**
   ```bash
   mkdir -p .claude/commands
   cat > .claude/commands/cppp.md << 'EOF'
   ---
   name: commit-pr
   description: Commit/PR workflow for <project>.
   ---

   Follows the standard cppp workflow. Project-specific deployment pipeline:

   ## Deployment
   <what happens after PR merge>

   ## Verify deployment
   <commands to check deployment status>
   EOF
   ```

3. **Commit to the repo** — agents and commands are version-controlled with the code, not stored separately.

---

## Day-to-day chezmoi workflow

```bash
# Edit a dotfile
chezmoi edit ~/.zshrc

# Preview changes before applying
chezmoi diff

# Apply to home directory
chezmoi apply

# Save and sync to all machines
cd ~/.local/share/chezmoi
git add -A && git commit -m "chore: <what changed>" && git push

# Pull updates on another machine
chezmoi update
```

## Adding a new secret

```bash
# Store in 1Password
op item create --category="API Credential" --title="My Service" \
  --vault="Personal" "credential=abc123"

# Reference in a template file
{{ onepasswordRead "op://Personal/My Service/credential" }}

# Apply
chezmoi apply
```

---

## Applying this to a professional team

The same pattern scales to team use. Key adaptations:

| Personal | Professional |
|----------|-------------|
| `~/.claude/agents/` per developer | Shared `.claude/agents/` committed to each repo |
| Secrets in personal 1Password | Secrets in team 1Password vault or CI secret manager |
| `silverbeer/dotfiles` | Team dotfiles repo or onboarding runbook |
| Global `/qe` command | Same — commit to team dotfiles |
| Project `.claude/agents/qe-engineer.md` | Same — commit to repo, whole team benefits |

**The golden rule for teams:** Anything in `.claude/` that's committed to the repo is automatically shared with every developer who clones it. Project-level agents and commands are free team productivity wins with zero setup.
