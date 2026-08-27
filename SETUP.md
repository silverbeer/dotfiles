# New Machine Setup Guide

Complete setup for a new Mac. Follow steps in order.

> **Platform:** macOS only. Requires Claude Code CLI installed and licensed.
> Install Claude Code: https://claude.ai/code

---

## Prerequisites

- macOS with [Homebrew](https://brew.sh) installed:
  ```bash
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
- Claude Code CLI installed and signed in
- 1Password desktop app installed and signed in

---

## Step 1 — Install chezmoi

```bash
brew install chezmoi
```

Other tools (`gh`, `jq`, `rtk`, `1password-cli`, `uv`) install automatically via chezmoi run scripts in Step 3.

Authenticate GitHub CLI **after** Step 3:
```bash
gh auth login
# Select: GitHub.com → HTTPS → Login with a web browser
```

---

## Step 2 — Enable 1Password CLI integration

1. Open 1Password desktop app
2. Settings (⌘,) → Developer
3. Enable **"Integrate with 1Password CLI"**

Verify it works:
```bash
op vault list
# Human terminal: should show Personal (and any shared vaults).
# An agent shell shows only `agents` — see Step 2b.
```

---

## Step 2b — Provision the agent service-account token

Claude Code sessions run `op` **headless**, with a 1Password service-account
token, because talking to the desktop app raises two prompts on every single
invocation (a macOS "access data from other apps" TCC prompt, and 1Password's
own CLI-access prompt, which never caches because Claude Code spawns a fresh
process per command).

This is the one credential that cannot bootstrap itself from 1Password, so it is
a manual step on every machine. Skipping it does not break anything visibly —
interactive shells keep working — it just means agent sessions on this Mac have
no `op` access at all.

1. 1Password.com → **Developer Tools → Service Accounts**
2. Create (or reuse) a service account with **read-only** access to the
   `agents` vault, and nothing else. Personal cannot be granted to a service
   account and must not be attempted.
3. Save the token, from a **human terminal**:
   ```bash
   mkdir -p ~/.config/op
   pbpaste > ~/.config/op/agent-token     # paste-based: keeps it off the shell history
   chmod 600 ~/.config/op/agent-token
   ```

Verify — from a Claude Code session, not this terminal:
```bash
op whoami       # User Type: SERVICE_ACCOUNT
op vault list   # exactly one vault: agents
```

`~/.zshenv` picks the token up automatically when `CLAUDECODE` is set or the
shell is non-interactive. It is never read by an interactive terminal, which
keeps biometrics for human sessions.

> Tokens expire. The current one expires **2026-11-19** — an auth failure near
> that date is expiry, not a bug. Reissue and overwrite the file.

---

## Step 3 — Apply dotfiles

```bash
chezmoi init --apply https://github.com/silverbeer/dotfiles
```

What it does:
- Clones `silverbeer/dotfiles` to `~/.local/share/chezmoi`
- Renders all templates
- Writes `~/.zshrc`, `~/.zshenv`, `~/.claude/` config, agents, commands

### Then point chezmoi at your working clone

`chezmoi init` clones to `~/.local/share/chezmoi`. If you also work on the repo
in `~/gitrepos/dotfiles`, those become **two independent clones** and chezmoi
keeps applying the one you are not editing — merged PRs stop reaching the
machine and nothing warns you. Pick one:

```bash
git clone https://github.com/silverbeer/dotfiles ~/gitrepos/dotfiles

cat >> ~/.config/chezmoi/chezmoi.toml <<'EOF'
sourceDir = "~/gitrepos/dotfiles"
EOF

rm -rf ~/.local/share/chezmoi     # only after confirming it holds no unpushed work
chezmoi source-path               # must print ~/gitrepos/dotfiles
```

`chezmoi.toml` is bootstrap config — it cannot manage itself, so this step is
per-machine and deliberately not tracked in the repo.

---

## Step 4 — Reload shell

```bash
source ~/.zshrc
```

---

## Step 5 — Set up RTK (Rust Token Killer)

RTK is a Claude Code hook that intercepts every Bash command and strips unnecessary output before it hits Claude's context window. **Saves 60-90% of tokens on git, gh, kubectl, ls, pytest, and more — transparently, with zero behavior change.**

The RTK hook is already configured in `~/.claude/settings.json` (applied by chezmoi in Step 3). Verify it's wired correctly:

```bash
rtk --version        # Should show: rtk 0.x.x
rtk gain             # Shows cumulative token savings
rtk init --show      # Confirms hook is installed
```

If the hook isn't active, re-initialize it:
```bash
rtk init -g          # Installs hook + RTK.md into ~/.claude
# Then restart Claude Code
```

---

## Step 5b — Linear API + CLI (agentic dev system)

The `linear-crud` skill drives Linear as the system of record. Two auth pieces
are per-machine:

```bash
# Linear CLI (issue CRUD) — interactive login, stores creds in the keychain
linear auth login
linear auth default silverbeer   # if it isn't already the default

# Linear personal API key (raw GraphQL: initiatives, cycles, estimates, metrics)
# Auto-created from 1Password by run_once_after_20-linear-api-key.sh on `chezmoi apply`.
# If op was locked at apply time, create it manually:
op read 'op://agents/linear_api_key/password' > ~/.config/linear/gql-key && chmod 600 ~/.config/linear/gql-key
```

The wrapper lives at `~/.claude/skills/linear-crud/scripts/linear-gql.sh` (synced
by chezmoi) and only ever talks to `api.linear.app`.

---

## Step 6 — Verify everything

The agentic-dev **doctor** checks the whole environment (tools, auth, Linear API,
skills) and is the fastest way to confirm a new machine matches:

```bash
bash ~/.claude/skills/linear-crud/scripts/doctor.sh
# Expect: all ✓, "N ok · 0 warn · 0 fail". WARN = a one-time interactive step
# (gh auth login / linear login / op signin) it prints the fix for.
```

Additional spot checks:

```bash
# RTK working
rtk gain

# 1Password CLI working (human terminal — Supabase MSA lives in Personal)
op item get "Supabase MSA" --fields credential --reveal

# 1Password CLI working from an AGENT shell (run inside Claude Code)
op whoami && op vault list      # SERVICE_ACCOUNT, one vault: agents

# Secrets provisioned to files rather than rendered into ~/.zshrc
ls -l ~/.config/linear/gql-key ~/.config/supabase/pg-url ~/.config/op/agent-token
# All three should be -rw------- and non-empty

# Claude agents and commands in place
ls ~/.claude/agents/
ls ~/.claude/commands/

# Neither zshrc nor zshenv holds a plaintext secret
grep -i "token\|password\|secret\|credential" ~/.zshrc ~/.zshenv
# Should show only op:// references and comments, nothing plaintext
```

---

## Day-to-day: making changes to dotfiles

Never edit `~/.zshrc` or `~/.claude/` files directly — always go through chezmoi:

```bash
# Edit a file
chezmoi edit ~/.zshrc

# Preview what will change
chezmoi diff

# Apply changes to home directory
chezmoi apply

# Commit and push to GitHub (syncs to all machines)
cd "$(chezmoi source-path)"        # not a hardcoded path — see Step 3
git add -A && git commit -m "chore: describe change" && git push
```

---

## Day-to-day: pulling updates on another machine

```bash
chezmoi update
# Pulls latest from GitHub and applies in one step
```

---

## Adding a new secret

Put it in the **`agents`** vault, not Personal — a service account can never be
granted Personal, so anything stored there is invisible to every Claude session.

```bash
# Store in 1Password (human terminal — the service account is read-only)
op item create --category="API Credential" --title="My Service" \
  --vault="agents" "credential=..."
```

Then provision it to a file with a `run_once_after_*` script, following
`run_once_after_20-linear-api-key.sh`, and export it from `dot_zshenv`.

**Prefer this over a `.tmpl` read.** A template renders on *every* chezmoi
invocation, so a failed read does not error — it degrades to an empty value and
`chezmoi apply` writes the file without the variable. That is exactly how
`SUPABASE_PG_URL` got silently dropped whenever an agent ran apply.

---

## Adding a new dotfile

```bash
chezmoi add ~/.someconfig
cd "$(chezmoi source-path)" && git add -A && git commit -m "add someconfig" && git push
```

---

## Files managed by chezmoi

| File | Notes |
|------|-------|
| `~/.zshrc` | Templated. Interactive shell config — holds no secrets |
| `~/.zshenv` | Every shell, incl. agents. Exports creds from 0600 files under `~/.config` |
| `~/.claude/CLAUDE.md` | Global Claude instructions |
| `~/.claude/RTK.md` | RTK config — loaded by Claude Code automatically |
| `~/.claude/settings.json` | Hooks (RTK rewrite), statusline, permissions |
| `~/.claude/agents/qe-engineer.md` | Global QE agent baseline |
| `~/.claude/commands/cppp.md` | `/cppp` slash command — commit/PR workflow |
| `~/.claude/commands/qe.md` | `/qe` slash command — coverage audit |

## Files NOT managed (intentionally ignored)

- `~/.claude/sessions/` — runtime data
- `~/.claude/history.jsonl` — conversation history
- `~/.claude/projects/` — per-project memory
- `~/.claude/skills/` — installed skills
- `~/.claude/settings.local.json` — machine-specific permission overrides
- `~/.config/op/agent-token` — service-account token, provisioned by hand (Step 2b)
- `~/.config/linear/gql-key`, `~/.config/supabase/pg-url` — secrets provisioned
  from 1Password by `run_once` scripts. Never commit these.
- `~/.config/chezmoi/chezmoi.toml` — bootstrap config, including `sourceDir`
