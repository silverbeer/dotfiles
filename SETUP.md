# New Machine Setup Guide

Complete setup for a new Mac. Follow steps in order.

---

## Prerequisites

- 1Password desktop app installed and signed in
- Homebrew installed: `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
- GitHub CLI authenticated: `brew install gh && gh auth login`

---

## Step 1 — Install tools

```bash
brew install chezmoi
brew install --cask 1password-cli
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

## Step 5 — Verify

```bash
# 1Password CLI working
op item get "Supabase MSA" --fields credential --reveal

# Claude config in place
ls ~/.claude/agents/
ls ~/.claude/commands/

# zshrc has no plaintext secrets
grep -i "token\|password\|secret\|credential" ~/.zshrc
# Should show only op:// references, nothing plaintext
```

---

## Day-to-day: making changes

Edit dotfiles via chezmoi — never edit `~/.zshrc` directly:

```bash
# Edit a file
chezmoi edit ~/.zshrc

# Preview what will change
chezmoi diff

# Apply changes to home directory
chezmoi apply

# Commit and push to GitHub
cd ~/.local/share/chezmoi
git add -A && git commit -m "your message" && git push
```

---

## Day-to-day: pulling updates on another machine

```bash
chezmoi update
# Pulls latest from GitHub and applies
```

---

## Adding a new secret

1. Add to 1Password: `op item create --category="API Credential" --title="My Service" --vault="Personal" "credential=abc123"`
2. Reference in template: `{{ onepasswordRead "op://Personal/My Service/credential" }}`
3. Apply: `chezmoi apply`

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
| `~/.claude/RTK.md` | RTK token-saving config |
| `~/.claude/settings.json` | Hooks, statusline, permissions |
| `~/.claude/mcp.json` | Templated — paths use homeDir |
| `~/.claude/agents/qe-engineer.md` | Global QE agent baseline |
| `~/.claude/commands/qe.md` | `/qe` slash command |

## Files NOT managed (intentionally ignored)

- `~/.claude/sessions/` — runtime data
- `~/.claude/history.jsonl` — conversation history
- `~/.claude/projects/` — per-project memory
- `~/.claude/skills/` — installed skills
- `~/.claude/settings.local.json` — machine-specific overrides
