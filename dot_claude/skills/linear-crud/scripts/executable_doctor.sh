#!/usr/bin/env bash
# doctor.sh — verify the agentic dev environment is fully set up on THIS machine.
# Run identically on every machine (MB Air, Mac mini, …) to confirm parity:
#   bash ~/.claude/skills/linear-crud/scripts/doctor.sh
#
# Checks toolchain, auth, and the Linear API wiring. Non-zero exit if anything
# is a hard FAIL. WARN = works but needs an interactive/one-time step.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Deployed it is linear-gql.sh; in the chezmoi source tree it carries the
# executable_ prefix (same resolution as sibling() in linear.sh).
GQL="$SCRIPT_DIR/linear-gql.sh"
[ -f "$GQL" ] || GQL="$SCRIPT_DIR/executable_linear-gql.sh"
KEY_FILE="${LINEAR_KEY_FILE:-$HOME/.config/linear/gql-key}"

pass=0 warn=0 fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
warnf(){ printf '  \033[33m!\033[0m %s\n     ↳ %s\n' "$1" "$2"; warn=$((warn+1)); }
failf(){ printf '  \033[31m✗\033[0m %s\n     ↳ %s\n' "$1" "$2"; fail=$((fail+1)); }

echo "── binaries ─────────────────────────────"
for bin in git gh jq curl uv node op linear rtk chezmoi; do
  if command -v "$bin" >/dev/null 2>&1; then ok "$bin"; else failf "$bin missing" "install via chezmoi run_onchange (brew)"; fi
done

echo "── auth ─────────────────────────────────"
if gh auth status >/dev/null 2>&1; then ok "gh authenticated"; else warnf "gh not authenticated" "run: gh auth login"; fi
if op account list >/dev/null 2>&1 && [ -n "$(op account list 2>/dev/null)" ]; then ok "op account configured"; else warnf "op not configured/signed in" "run: op signin (needed to bootstrap the Linear key)"; fi
if [ -f "$HOME/.config/linear/credentials.toml" ] && grep -q '^default' "$HOME/.config/linear/credentials.toml" 2>/dev/null; then
  ok "linear CLI has a default workspace"
else
  warnf "linear CLI not logged in" "run: linear auth login"
fi

echo "── Linear API (raw GraphQL) ─────────────"
if [ -f "$KEY_FILE" ]; then
  perms="$(stat -f '%A' "$KEY_FILE" 2>/dev/null || stat -c '%a' "$KEY_FILE" 2>/dev/null)"
  # shellcheck disable=SC2015 # ok/warnf always return 0, so the || branch can only fire on a false test
  [ "$perms" = "600" ] && ok "gql-key present (perms 600)" || warnf "gql-key perms $perms" "chmod 600 $KEY_FILE"
  gql_err="$(mktemp "${TMPDIR:-/tmp}/doctor-gql.XXXXXX")"
  if resp="$(bash "$GQL" '{ viewer { id name } }' 2>"$gql_err")" && printf '%s' "$resp" | jq -e '.data.viewer.id' >/dev/null 2>&1; then
    who="$(printf '%s' "$resp" | jq -r '.data.viewer.name')"
    ok "linear-gql.sh authenticates ($who)"
  else
    # Classify from the wrapper's own message: a 401/403 is the key, anything
    # else (curl failure, 5xx) is Linear itself — re-creating the key won't help.
    err="$(head -c 300 "$gql_err")"
    case "$err" in
      *"HTTP 401"*|*"HTTP 403"*) hint="key invalid/expired — re-create gql-key from op (see below)" ;;
      *"HTTP 000"*|*"HTTP 5"*)   hint="Linear unreachable / outage — not a key problem" ;;
      *)                          hint="see message above" ;;
    esac
    failf "linear-gql.sh API call failed: ${err:-no output}" "$hint"
  fi
  rm -f "$gql_err"
else
  failf "gql-key missing at $KEY_FILE" "op read 'op://agents/linear_api_key/password' > $KEY_FILE  (then chmod 600)"
fi

echo "── skills + repos ───────────────────────"
for s in linear-crud todo session-audit; do
  # shellcheck disable=SC2015 # ok/warnf always return 0, so the || branch can only fire on a false test
  [ -d "$HOME/.claude/skills/$s" ] && ok "skill: $s" || warnf "skill $s not synced" "run: chezmoi apply"
done
# shellcheck disable=SC2015 # ok/warnf always return 0, so the || branch can only fire on a false test
[ -d "$HOME/gitrepos" ] && ok "$HOME/gitrepos present" || warnf "$HOME/gitrepos missing" "clone your repos under $HOME/gitrepos"

echo "─────────────────────────────────────────"
printf 'summary: \033[32m%d ok\033[0m · \033[33m%d warn\033[0m · \033[31m%d fail\033[0m\n' "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ]
