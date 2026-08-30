#!/usr/bin/env bash
# Source this before gate.py open/poll/resolve: exports GATEKEEPER_TG_TOKEN and
# GATEKEEPER_TG_CHAT_ID from 1Password if they are not already set.
#
#   source ~/.claude/skills/gatekeeper/scripts/env.sh
#
# Never echoes a value. If `op` is missing or locked, this is a silent no-op —
# gate.py's own gatekeeper_from_env() gives the actionable error ("source
# scripts/env.sh") when the vars really are needed and still unset.
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
