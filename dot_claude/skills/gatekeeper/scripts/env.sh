#!/usr/bin/env bash
# Source this before gate.py open/poll/resolve: exports GATEKEEPER_TG_TOKEN and
# GATEKEEPER_TG_CHAT_ID from 1Password if they are not already set.
#
#   source ~/.claude/skills/gatekeeper/scripts/env.sh
#
# Never echoes a value. If `op` is missing or locked, this is a silent no-op —
# gate.py's own gatekeeper_from_env() gives the actionable error ("source
# scripts/env.sh") when the vars really are needed and still unset.
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

if [ -z "${GATEKEEPER_TG_TOKEN:-}" ] && command -v op >/dev/null 2>&1; then
  if token="$(op read 'op://agents/cycle-runner-telegram/token' 2>/dev/null)" && [ -n "$token" ]; then
    export GATEKEEPER_TG_TOKEN="$token"
  fi
  unset token
fi

if [ -z "${GATEKEEPER_TG_CHAT_ID:-}" ] && command -v op >/dev/null 2>&1; then
  if chat_id="$(op read 'op://agents/cycle-runner-telegram/chat-id' 2>/dev/null)" && [ -n "$chat_id" ]; then
    export GATEKEEPER_TG_CHAT_ID="$chat_id"
  fi
  unset chat_id
fi
