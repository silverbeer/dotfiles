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
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && command -v op >/dev/null 2>&1; then
  if token="$(op read 'op://agents/cycle-runner-claude/token' 2>/dev/null)" && [ -n "$token" ]; then
    export CLAUDE_CODE_OAUTH_TOKEN="$token"
  fi
  unset token
fi
