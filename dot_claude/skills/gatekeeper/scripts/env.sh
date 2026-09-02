#!/usr/bin/env bash
# Source this before gate.py open/poll/resolve: exports GATEKEEPER_TG_TOKEN and
# GATEKEEPER_TG_CHAT_ID from local files.
#
#   source ~/.claude/skills/gatekeeper/scripts/env.sh
#
# Never echoes a value. A missing file is a silent no-op — gate.py's own
# gatekeeper_from_env() gives the actionable error when the vars really are
# needed and still unset.
#
# ---------------------------------------------------------------- SB-974
# `op` is NOT called here, and must not be. See
# cycle-runner/scripts/env.sh for the full reasoning: once a desktop account
# exists in ~/.config/op/config, `op read` prefers it over a valid
# service-account token and raises a Touch ID prompt, and a wedged `op` blocks
# every later call — which stops an unattended runner dead.
#
# Provision these files with:
#
#   bash ~/.claude/skills/cycle-runner/scripts/provision-secrets.sh
CR_SECRETS_DIR="${CYCLE_RUNNER_SECRETS_DIR:-$HOME/.config/cycle-runner}"

if [ -z "${GATEKEEPER_TG_TOKEN:-}" ] && [ -r "$CR_SECRETS_DIR/telegram-token" ]; then
  GATEKEEPER_TG_TOKEN="$(cat "$CR_SECRETS_DIR/telegram-token")"
  export GATEKEEPER_TG_TOKEN
fi

if [ -z "${GATEKEEPER_TG_CHAT_ID:-}" ] && [ -r "$CR_SECRETS_DIR/telegram-chat-id" ]; then
  GATEKEEPER_TG_CHAT_ID="$(cat "$CR_SECRETS_DIR/telegram-chat-id")"
  export GATEKEEPER_TG_CHAT_ID
fi
