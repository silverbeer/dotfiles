#!/usr/bin/env bash
# Source this before run.sh: exports CLAUDE_CODE_OAUTH_TOKEN from 1Password if
# it is not already set.
#
#   source ~/.claude/skills/cycle-runner/scripts/env.sh
#
# Never echoes a value. If `op` is missing or locked, this is a silent no-op —
# run.sh's own claude_token() gives the actionable error ("source
# scripts/env.sh") when the token really is needed and still unset. Same shape
# as gatekeeper/scripts/env.sh, one vault item, one var.
#
# Vault item: op://agents/cycle-runner-claude/token — a `claude setup-token`
# 1-year token for the dedicated cycle-runner identity, not a personal one
# (SB-929 design decision: naming mirrors cycle-runner-telegram).
# The service-account token, for processes launchd starts (SB-953).
#
# ~/.zshenv exports OP_SERVICE_ACCOUNT_TOKEN for agent shells, but launchd does
# not source it — the cycle-runner plist passes only HOME and PATH. Without the
# token `op` falls back to the 1Password DESKTOP APP, which raised "op would
# like to access data from other apps" and a Touch ID prompt on every tick, and
# fails outright when nobody is at the machine. Reading the file here is what
# makes an unattended tick actually headless.
#
# Deliberately NOT in the plist: that file is chezmoi-managed and world
# readable, and a bearer token must not live in it.
if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] && [ -r "$HOME/.config/op/agent-token" ]; then
    OP_SERVICE_ACCOUNT_TOKEN="$(cat "$HOME/.config/op/agent-token")"
    export OP_SERVICE_ACCOUNT_TOKEN
fi

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && command -v op >/dev/null 2>&1; then
  if token="$(op read 'op://agents/cycle-runner-claude/token' 2>/dev/null)" && [ -n "$token" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$token"
  fi
  unset token
fi
