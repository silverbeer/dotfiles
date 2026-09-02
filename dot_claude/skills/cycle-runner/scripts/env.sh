#!/usr/bin/env bash
# Source this before run.sh: exports CLAUDE_CODE_OAUTH_TOKEN from a local file.
#
#   source ~/.claude/skills/cycle-runner/scripts/env.sh
#
# Never echoes a value. A missing file is a silent no-op — run.sh's own check
# gives the actionable error when the token really is needed and still unset.
#
# ---------------------------------------------------------------- SB-974
# `op` is NOT called here, and must not be. Two earlier fixes (SB-953, SB-972)
# assumed that exporting OP_SERVICE_ACCOUNT_TOKEN makes `op` headless. It does
# not: once a desktop account exists in ~/.config/op/config — which happened
# the moment a human ran `op signin` to create these very items — `op read`
# prefers the desktop app and raises a Touch ID prompt, with a valid service
# account token set. Proven with a logging shim: `token_set=yes` on every call,
# and one `op read` stayed wedged for twelve minutes.
#
# Worse, a wedged `op` blocks every later call, so an unanswered prompt on an
# unattended machine stops the runner entirely (SB-868).
#
# The Linear key has never had this problem because it is provisioned once to a
# file and read as a file. This does the same. Provision with:
#
#   bash ~/.claude/skills/cycle-runner/scripts/provision-secrets.sh
#
# which is the ONLY thing here that talks to 1Password, is run by a human in a
# real terminal, and is never on a tick's path.
CR_SECRETS_DIR="${CYCLE_RUNNER_SECRETS_DIR:-$HOME/.config/cycle-runner}"

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -r "$CR_SECRETS_DIR/claude-token" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(cat "$CR_SECRETS_DIR/claude-token")"
  export CLAUDE_CODE_OAUTH_TOKEN
fi
