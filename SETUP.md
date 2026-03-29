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

## Step 1 — Install tools

```bash
brew install chezmoi gh rtk
brew install --cask 1password-cli
```

Authenticate GitHub CLI:
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
# Should show: Personal
```

---

## Step 3 — Apply dotfiles

This one command clones the dotfiles repo and applies everything:

```bash
chezmoi init --apply https://github.com/silverbeer/dotfiles
```

What it does:
- Clones `silverbeer/dotfiles` to `~/.local/share/chezmoi`
- Renders all templates (pulls secrets live from 1Password)
- Writes `~/.zshrc`, `~/.claude/` config, agents, commands

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

## Step 6 — Verify everything

```bash
# RTK working
rtk gain

# 1Password CLI working
op item get "Supabase MSA" --fields credential --reveal

# Claude agents and commands in place
ls ~/.claude/agents/
ls ~/.claude/commands/

# zshrc has no plaintext secrets
grep -i "token\|password\|secret\|credential" ~/.zshrc
# Should show only op:// references, nothing plaintext
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
cd ~/.local/share/chezmoi
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

```bash
# Store in 1Password
op item create --category="API Credential" --title="My Service" \
  --vault="Personal" "credential=abc123"

# Reference in a template file (.tmpl extension)
{{ onepasswordRead "op://Personal/My Service/credential" }}

# Apply
chezmoi apply
```

---

## Adding a new dotfile

```bash
chezmoi add ~/.someconfig
cd ~/.local/share/chezmoi && git add -A && git commit -m "add someconfig" && git push
```

---

## Files managed by chezmoi

| File | Notes |
|------|-------|
| `~/.zshrc` | Templated — secrets via 1Password |
| `~/.claude/CLAUDE.md` | Global Claude instructions |
| `~/.claude/RTK.md` | RTK config — loaded by Claude Code automatically |
| `~/.claude/settings.json` | Hooks (RTK rewrite), statusline, permissions |
| `~/.claude/mcp.json` | Templated — paths use homeDir |
| `~/.claude/agents/qe-engineer.md` | Global QE agent baseline |
| `~/.claude/commands/cppp.md` | `/cppp` slash command — commit/PR workflow |
| `~/.claude/commands/qe.md` | `/qe` slash command — coverage audit |

## Files NOT managed (intentionally ignored)

- `~/.claude/sessions/` — runtime data
- `~/.claude/history.jsonl` — conversation history
- `~/.claude/projects/` — per-project memory
- `~/.claude/skills/` — installed skills
- `~/.claude/settings.local.json` — machine-specific permission overrides
