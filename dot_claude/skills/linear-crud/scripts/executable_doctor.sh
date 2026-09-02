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
# The one machine cycle-runner actually runs on. Must match
# Library/LaunchAgents/io.silverbeer.cycle-runner.plist.tmpl's own
# `{{ if eq .chezmoi.hostname "Toms-Mac-mini" }}` gate — defined once here so
# the two never drift apart into silently checking different things.
CYCLE_RUNNER_HOST="Toms-Mac-mini"

# The live `claude -p` probe below is a real billed call, so it is pinned and
# capped. The cap must clear the system-prompt preamble: at $0.02 the call was
# killed before it could answer and the check FAILED for every token, valid or
# not (SB-942). Pinning the model keeps the probe's cost independent of
# whatever the default model happens to become.
DOCTOR_PROBE_MODEL="claude-haiku-4-5-20251001"
DOCTOR_PROBE_BUDGET_USD="0.10"

# A peer skill's scripts dir, resolved the same way run.sh's own
# sibling_scripts() does: try the chezmoi source tree layout, then the
# deployed ~/.claude one, and print whichever actually has the marker file.
# Prints the dir on stdout, returns 1 (silently — every caller here is a
# machine that may legitimately not have the skill provisioned) if neither
# candidate has it.
sibling_scripts() {
  local skill="$1" marker="$2" f
  for f in "$SCRIPT_DIR/../../$skill/scripts/$marker" "$HOME/.claude/skills/$skill/scripts/$marker"; do
    if [ -f "$f" ]; then dirname "$f"; return 0; fi
  done
  return 1
}

pass=0 warn=0 fail=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
warnf(){ printf '  \033[33m!\033[0m %s\n     ↳ %s\n' "$1" "$2"; warn=$((warn+1)); }
failf(){ printf '  \033[31m✗\033[0m %s\n     ↳ %s\n' "$1" "$2"; fail=$((fail+1)); }
# Purely informational — a check that does not apply on this machine at all
# (wrong host for a host-gated check). Does not move pass/warn/fail: it was
# never run, so it cannot count as either a pass or a problem.
infof(){ printf '  \033[36mi\033[0m %s\n' "$1"; }

echo "── binaries ─────────────────────────────"
for bin in git gh jq curl uv node op linear rtk chezmoi gitleaks; do
  if command -v "$bin" >/dev/null 2>&1; then ok "$bin"; else failf "$bin missing" "install via chezmoi run_onchange (brew)"; fi
done

echo "── auth ─────────────────────────────────"
# Captured once and reused by the cycle-runner section's summary line below —
# `gh auth status` used to be shelled out to twice per run for the same answer.
gh_auth_out="$(gh auth status 2>&1)"
gh_auth_rc=$?
if [ "$gh_auth_rc" -eq 0 ]; then ok "gh authenticated"; else warnf "gh not authenticated" "run: gh auth login"; fi
# doctor does NOT call `op` (SB-974). Two earlier fixes assumed that exporting
# OP_SERVICE_ACCOUNT_TOKEN makes `op` headless; it does not, once a desktop
# account exists in ~/.config/op/config. A logging shim caught `op read` going
# to the desktop app with a valid token set, and one call stayed wedged for
# twelve minutes — and a wedged `op` blocks every later call. A health check
# that can hang the machine it is checking is worse than no check.
#
# Secrets are provisioned to files instead; what doctor verifies is the files.
CR_SECRETS_DIR="${CYCLE_RUNNER_SECRETS_DIR:-$HOME/.config/cycle-runner}"
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
# The repo <-> label map linear.sh, board.py and metrics.sh all read. Deployed
# next to SKILL.md; in the chezmoi source tree it sits beside scripts/ too.
REPOS_JSON="$SCRIPT_DIR/../repos.json"
if [ ! -f "$REPOS_JSON" ]; then
  failf "repos.json missing at $REPOS_JSON" "it ships with the linear-crud skill — run: chezmoi apply"
elif jq -e 'type=="array" and length>0' "$REPOS_JSON" >/dev/null 2>&1; then
  ok "repos.json present and parses ($(jq -r 'length' "$REPOS_JSON") repos)"
else
  failf "repos.json does not parse as a non-empty array" "fix $REPOS_JSON (jq . to see the error)"
fi

echo "── cycle-runner (Mac mini) ───────────────"
# These checks cover what run.sh needs to actually fire on a launchd tick
# (SB-930). None of them are hard requirements on a laptop that never runs
# cycle-runner — WARN, not FAIL, when the machine legitimately has no reason
# to be provisioned for it.

# .chezmoi.hostname the same way the plist template gates on it (see
# CYCLE_RUNNER_HOST above) — NOT the OS hostname directly, so this always asks
# the same question chezmoi apply already answered when it did or didn't
# deploy the plist. A `chezmoi` that is missing or fails resolves to the empty
# string, which safely reads as "not the mini" rather than crashing the check.
CURRENT_HOSTNAME="$(chezmoi execute-template '{{ .chezmoi.hostname }}' 2>/dev/null || true)"
on_mini=0
[ "$CURRENT_HOSTNAME" = "$CYCLE_RUNNER_HOST" ] && on_mini=1

if [ "$on_mini" -eq 1 ]; then
  CR_SCRIPTS="$(sibling_scripts cycle-runner env.sh || true)"
  if [ -n "$CR_SCRIPTS" ]; then
    # shellcheck source=/dev/null
    . "$CR_SCRIPTS/env.sh"
  fi

  # The check that would have caught SB-953. Everything else in this section
  # runs in a shell that ALREADY has OP_SERVICE_ACCOUNT_TOKEN (via ~/.zshenv),
  # so it cannot see that launchd — which passes only HOME and PATH — had no
  # token at all and sent `op` to the desktop app, raising a Touch ID prompt
  # every 30 minutes and failing outright with nobody at the machine.
  # What a tick actually needs is three files. Checked in a launchd-shaped
  # `env -i` — only HOME and PATH, exactly what the plist provides — so this
  # proves a TICK can start, not that the operator's richer shell can.
  for secret in claude-token telegram-token telegram-chat-id; do
    f="$CR_SECRETS_DIR/$secret"
    if [ ! -r "$f" ]; then
      failf "missing $f" "run: bash ~/.claude/skills/cycle-runner/scripts/provision-secrets.sh (from a real terminal)"
    elif [ "$(stat -f '%Lp' "$f" 2>/dev/null || stat -c '%a' "$f" 2>/dev/null)" != "600" ]; then
      warnf "$f is not 0600" "chmod 600 $f"
    else
      ok "secret present: $secret ($(wc -c <"$f" | tr -d ' ') bytes, 0600)"
    fi
  done

  if [ -n "$CR_SCRIPTS" ]; then
    # shellcheck disable=SC2016  # $CR expands in the INNER shell
    tick_token="$(env -i HOME="$HOME" PATH="$PATH" CR="$CR_SCRIPTS" bash -c '
      . "$CR/env.sh" 2>/dev/null
      [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && echo present
    ' 2>/dev/null || true)"
    if [ "$tick_token" = "present" ]; then
      ok "launchd-shaped env gets the claude token from disk (no op, no prompt)"
    else
      failf "a launchd-shaped env does not get CLAUDE_CODE_OAUTH_TOKEN" \
        "run provision-secrets.sh — a tick cannot authenticate without it"
    fi
  fi

  # A regression here is silent and expensive: `op` on a tick path can wedge on
  # an unanswered desktop prompt and stop the runner (SB-868, SB-974).
  if [ -n "$CR_SCRIPTS" ] && grep -qE '^[^#]*\bop (read|item|whoami|account)\b' "$CR_SCRIPTS/env.sh" 2>/dev/null; then
    failf "cycle-runner/env.sh calls op on the tick path" "secrets must come from files — see SB-974"
  else
    ok "no op invocation on the tick path"
  fi

  if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    warnf "CLAUDE_CODE_OAUTH_TOKEN not set" "source cycle-runner/scripts/env.sh (op://agents/cycle-runner-claude/token) or export it"
  elif ! command -v claude >/dev/null 2>&1; then
    failf "claude not on PATH" "install it — cycle-runner cannot start without it"
  else
    # `claude --help` on 2.1.251 has no --max-turns (the exact gap SB-929's
    # run.sh already flags for `claude -p`) — --tools "" (disables every tool,
    # so this can never touch Bash/Edit/etc), --max-budget-usd (hard dollar
    # cap) and --no-session-persistence (leaves nothing to clean up) are the
    # real bounded/non-interactive flags `claude --help` actually documents.
    if claude_out="$(claude -p "ok" --tools "" --model "$DOCTOR_PROBE_MODEL" \
        --max-budget-usd "$DOCTOR_PROBE_BUDGET_USD" --no-session-persistence \
        --output-format text 2>&1)"; then
      ok "claude OAuth token valid ($(printf '%s' "$claude_out" | tr '\n' ' ' | cut -c1-60))"
    elif printf '%s' "$claude_out" | grep -q 'Exceeded USD budget'; then
      # Hitting the ceiling means the call authenticated and started billing,
      # which is the very thing this check exists to prove. Never report that
      # as a credential failure — raise the cap instead.
      warnf "claude -p hit the \$$DOCTOR_PROBE_BUDGET_USD probe cap — token authenticated" \
        "raise DOCTOR_PROBE_BUDGET_USD in doctor.sh if this persists"
    else
      failf "claude -p failed with CLAUDE_CODE_OAUTH_TOKEN set" "$(printf '%s' "$claude_out" | head -1)"
    fi
  fi
else
  # This is the whole point of the gate: `claude -p` is a real, billed API
  # call. Every machine that isn't the mini must never reach it, no matter
  # what CLAUDE_CODE_OAUTH_TOKEN happens to be set to.
  infof "not the cycle-runner host, skipping live claude -p check"
fi

# Reuses the "── auth ──" section's gh auth status call above instead of
# shelling out to gh a second time for the same answer.
if [ "$gh_auth_rc" -eq 0 ]; then
  ok "gh auth status: $(printf '%s' "$gh_auth_out" | grep -m1 'Logged in' | sed 's/^ *//')"
else
  warnf "gh auth status failed" "$(printf '%s' "$gh_auth_out" | head -1) — run: gh auth login"
fi

# The self-check for the plist itself, ON THE MINI ONLY: guards the exact
# hostname-drift scenario (OS reinstall/rename) that would otherwise leave
# every other check green while cycle-runner silently never fires again.
if [ "$on_mini" -eq 1 ]; then
  PLIST_FILE="$HOME/Library/LaunchAgents/io.silverbeer.cycle-runner.plist"
  if [ ! -f "$PLIST_FILE" ]; then
    failf "cycle-runner plist missing at $PLIST_FILE" "run: chezmoi apply — this host reports as $CYCLE_RUNNER_HOST but never got the plist"
  else
    ok "cycle-runner plist present at $PLIST_FILE"
    if launchctl list io.silverbeer.cycle-runner >/dev/null 2>&1; then
      ok "cycle-runner plist loaded in launchctl"
    else
      warnf "cycle-runner plist not loaded in launchctl" "run: launchctl load -w $PLIST_FILE (one-time manual step)"
    fi
  fi
else
  infof "not the cycle-runner host, skipping cycle-runner plist deployment check"
fi

# GK_SCRIPTS (skill presence) is checked BEFORE the token: with the skill
# absent, GATEKEEPER_TG_TOKEN is also unset (env.sh never sourced), so
# checking the token first meant the more specific "skill not found" branch
# could never fire — the "not set" branch always won.
GK_SCRIPTS="$(sibling_scripts gatekeeper tg.py || true)"
if [ -n "$GK_SCRIPTS" ] && [ -f "$GK_SCRIPTS/env.sh" ]; then
  # shellcheck source=/dev/null
  . "$GK_SCRIPTS/env.sh"
fi
if [ -z "$GK_SCRIPTS" ]; then
  warnf "gatekeeper skill not found" "cannot import tg.py — run: chezmoi apply"
elif [ -z "${GATEKEEPER_TG_TOKEN:-}" ]; then
  warnf "GATEKEEPER_TG_TOKEN not set" "gatekeeper creds not provisioned on this machine — skipping Telegram getMe (fine off the mini)"
else
  if tg_who="$(GATEKEEPER_TG_TOKEN="$GATEKEEPER_TG_TOKEN" python3 - "$GK_SCRIPTS" <<'PY' 2>&1
import os
import sys

sys.path.insert(0, sys.argv[1])
from tg import TelegramError, TelegramTransport  # noqa: E402

try:
    me = TelegramTransport(os.environ["GATEKEEPER_TG_TOKEN"]).get_me()
    print(me.get("username") or me.get("first_name") or "?")
except TelegramError as e:
    print(e)
    sys.exit(1)
PY
)"; then
    ok "Telegram getMe ok (@$tg_who)"
  else
    failf "Telegram getMe failed" "$tg_who"
  fi
fi

LOCK_PID_FILE="$HOME/.local/state/cycle-runner/lock/pid"
if [ -f "$LOCK_PID_FILE" ]; then
  lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    ok "cycle-runner lock held by running pid $lock_pid (a run is in progress)"
  else
    warnf "cycle-runner lock present but pid '${lock_pid:-<empty>}' is not running" "looks abandoned — informational only; run.sh clears stale locks itself on its next tick, doctor does not touch it"
  fi
else
  ok "no cycle-runner lock present"
fi

echo "── chezmoi sync ──────────────────────────"
# The actual root-cause fix for PR #37 silently reverting PR #35: that
# happened because `chezmoi re-add` was run on a machine whose ~/.claude was
# already behind the source tree, so the re-add captured the STALE deployed
# copy and wrote it back over someone else's already-merged work. This is not
# cycle-runner specific — it must fail loudly before ANY re-add, always.
if command -v chezmoi >/dev/null 2>&1; then
  if cs_out="$(chezmoi status 2>&1)"; then
    if [ -z "$cs_out" ]; then
      ok "chezmoi status clean — safe to re-add"
    else
      failf "chezmoi status is NOT clean" "this machine's ~/.claude is not in sync with the source tree — DO NOT run chezmoi re-add until you've reconciled with 'chezmoi diff', or you risk reverting someone else's merged work (this happened: PR #37 silently reverted PR #35)"
    fi
  else
    failf "chezmoi status failed to run" "$(printf '%s' "$cs_out" | head -1)"
  fi
else
  warnf "chezmoi not on PATH" "cannot check sync status"
fi

echo "─────────────────────────────────────────"
printf 'summary: \033[32m%d ok\033[0m · \033[33m%d warn\033[0m · \033[31m%d fail\033[0m\n' "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ]
