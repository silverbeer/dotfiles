#!/usr/bin/env bash
# One-time: bootstrap the Supabase connection string used by the supabase-sql
# MCP server. Mirrors run_once_after_20-linear-api-key.sh.
#
# Why this is a file and not a chezmoi template any more: the item lives in the
# built-in Personal vault, and a 1Password service account can NEVER be granted
# Personal. So under an agent shell (which exports OP_SERVICE_ACCOUNT_TOKEN) the
# template's `op read` failed, degraded to an empty value, and `chezmoi apply`
# rewrote ~/.zshrc *without* SUPABASE_PG_URL — silently breaking the MCP server
# for the interactive shell. Provisioning to a file once, and exporting it from
# ~/.zshenv, means apply never renders the secret and so can never drop it.
#
# run_once_BEFORE, not after: as an after-script it would run once chezmoi had
# already rewritten ~/.zshrc without the export, and the migration path below
# would find nothing — losing the value on exactly the machines that have it.
set -uo pipefail
umask 077

KEY_FILE="$HOME/.config/supabase/pg-url"
OP_REF='op://Personal/Supabase MSA/credential'
OP_TIMEOUT=15

mkdir -p "$(dirname "$KEY_FILE")"

if [ -s "$KEY_FILE" ]; then
  echo "supabase pg-url already present — skipping"
  exit 0
fi
rm -f "$KEY_FILE"   # a zero-byte leftover must not look like success

install_key() {  # stdin -> $KEY_FILE, atomically; fails if empty
  local tmp; tmp="$(mktemp "${KEY_FILE}.XXXXXX")" || return 1
  cat >"$tmp"
  if [ -s "$tmp" ]; then chmod 600 "$tmp"; mv "$tmp" "$KEY_FILE"; return 0; fi
  rm -f "$tmp"; return 1
}

# Bounded `op read`. `op` can wedge indefinitely (SB-868), and an unbounded read
# here would hang `chezmoi apply` itself with no output. macOS ships no
# `timeout`/`gtimeout`, so this is done by hand: background the read, poll, kill.
op_read_bounded() {
  local out; out="$(mktemp)" || return 1
  op read "$OP_REF" >"$out" 2>/dev/null &
  local pid=$! i=0
  while [ "$i" -lt "$OP_TIMEOUT" ]; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1; i=$((i + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    rm -f "$out"
    echo "  (op read timed out after ${OP_TIMEOUT}s — see SB-868)" >&2
    return 1
  fi
  wait "$pid" 2>/dev/null
  [ -s "$out" ] && cat "$out" && rm -f "$out" && return 0
  rm -f "$out"; return 1
}

# Preferred: read it from 1Password. Only works in a human terminal, since
# Personal is desktop-app/biometric only.
if command -v op >/dev/null 2>&1 && op_read_bounded | install_key; then
  echo "supabase pg-url created from 1Password ($KEY_FILE)"
  exit 0
fi

# Migration: on a machine that predates this change the value is already in the
# environment, or still exported from the old ~/.zshrc. Lift it across rather
# than making the user re-fetch a secret they already have.
if [ -n "${SUPABASE_PG_URL:-}" ] && printf '%s\n' "$SUPABASE_PG_URL" | install_key; then
  echo "supabase pg-url migrated from the environment ($KEY_FILE)"
  exit 0
fi

if [ -r "$HOME/.zshrc" ] &&
   sed -n 's/^export SUPABASE_PG_URL=//p' "$HOME/.zshrc" | head -1 | sed 's/^"//; s/"$//' | install_key; then
  echo "supabase pg-url migrated from existing ~/.zshrc ($KEY_FILE)"
  exit 0
fi

echo "WARN: could not create $KEY_FILE."
echo "  Personal is biometric-only, so run this from a HUMAN terminal"
echo "  (not an agent session), then re-check with the doctor:"
echo "    op read '$OP_REF' > $KEY_FILE && chmod 600 $KEY_FILE"
exit 0
