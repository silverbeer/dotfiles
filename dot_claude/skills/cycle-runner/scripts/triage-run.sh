#!/usr/bin/env bash
# cycle-runner/scripts/triage-run.sh — SB-624 / T9. ONE entry point, kicks off
# the weekly /triage batch, exits.
#
# Invoked by launchd's Monday calendar trigger (io.silverbeer.triage.plist).
# It never loops and it never drains gates itself:
#
#   1. Acquires its OWN lock ($STATE/triage-lock) — deliberately separate from
#      run.sh's $STATE/lock. run.sh's own ticket work can run up to ~30
#      minutes (SB-946); if this script shared that lock, a Monday 9am tick
#      landing mid-ticket would just silently skip the whole week's triage
#      run rather than waiting or queueing. Its own lock means the two never
#      contend at all.
#   2. Starts `claude --session-id S -p "/triage --session-id S --run-id R"`.
#      That command opens exactly one `gate.py` triage gate (or none, if
#      nothing needs triage) and exits — same "propose, gate, exit" shape as
#      `/work-headless`.
#   3. Releases the lock, writes a run log, gitleaks-scans it (same
#      fail-closed rule as run.sh's scan_log_clean — a missing scanner means
#      nothing is posted, not "post anyway"), and posts a short Telegram
#      summary via gatekeeper's own tg.py transport.
#
# Deliberately does NOT drain gates: once a human approves/rejects on
# Telegram or Linear, run.sh's own `gate.py poll --once` step (it polls EVERY
# awaiting gate, not just work-headless ones) finds the triage gate on its
# next regular tick and resumes this session generically — the same
# `resume_session` path an ordinary ticket gate uses. Duplicating that drain
# loop here would mean two processes racing to resume the same session id.
set -euo pipefail
export NO_COLOR=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${GATEKEEPER_STATE:-$HOME/.local/state/cycle-runner}"
RUN_LOG_DIR="$STATE/logs"
LOCK_DIR="$STATE/triage-lock"

die() { echo "triage-run: $*" >&2; exit 1; }
note() { echo "triage-run: $*"; }

# Same resolution dance as run.sh's sibling_scripts(): chezmoi source tree
# layout first, then the deployed ~/.claude one.
sibling_scripts() {
  local skill="$1" marker="$2" f
  for f in "$SCRIPT_DIR/../../$skill/scripts/$marker" "$HOME/.claude/skills/$skill/scripts/$marker"; do
    [[ -f "$f" ]] && { dirname "$f"; return 0; }
  done
  die "$skill/scripts/$marker not found — the $skill skill must be installed"
}

gen_uuid() {
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr '[:upper:]' '[:lower:]'
  else
    python3 -c 'import uuid; print(uuid.uuid4())'
  fi
}

# --------------------------------------------------------------------- lock
#
# Byte-identical logic to run.sh's own lock (mkdir-atomic test-and-set, pid
# staleness, created_at grace window) — deliberately duplicated rather than
# sourced: the two scripts must never share a lock DIRECTORY (that's the
# whole point of this script existing), and there is no third file to source
# both from without inventing one just for this.
LOCK_GRACE_SECS=10

release_lock() { rm -rf "$LOCK_DIR"; }

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    date +%s >"$LOCK_DIR/created_at"
    echo $$ >"$LOCK_DIR/pid"
    trap release_lock EXIT
    return 0
  fi
  local pid
  pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    note "lock held by running pid $pid — another triage-run is in progress, exiting quietly"
    exit 0
  fi
  if [[ -z "$pid" ]]; then
    local created now age
    created="$(cat "$LOCK_DIR/created_at" 2>/dev/null || true)"
    now="$(date +%s)"
    [[ -z "$created" ]] && created="$now"
    age=$((now - created))
    if [[ "$age" -lt "$LOCK_GRACE_SECS" ]]; then
      note "lock dir exists with no pid yet (${age}s old, under ${LOCK_GRACE_SECS}s grace) — holder is still writing its pid, backing off quietly"
      exit 0
    fi
  fi
  note "stale lock found (pid ${pid:-?} is not running) — removing it and retrying"
  rm -rf "$LOCK_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ >"$LOCK_DIR/pid"
    trap release_lock EXIT
    return 0
  fi
  note "lost the race for the lock after clearing a stale one — exiting quietly"
  exit 0
}

# ----------------------------------------------------------------- claude -p

# /triage's own output contract (triage.md's "Output contract" section) —
# NOT work-headless's branch/pr_url schema, since /triage never branches or
# opens a PR.
JSON_SCHEMA='{"type":"object","properties":{"status":{"type":"string","enum":["clean","awaiting","applied","rejected","error"]},"applied_count":{"type":"integer"},"gate_id":{"type":["string","null"]}},"required":["status","applied_count","gate_id"]}'

# Same tool allowlist as run.sh's work-headless invocation: triage.py never
# shells out to git or gh at all, but this is defense in depth, not a
# behavioural difference — a stray tool call outside this list fails closed
# either way.
ALLOWED_TOOLS="Bash,Read,Edit,Write,Grep,Glob,Agent"
DISALLOWED_TOOLS="Bash(git push --force*),Bash(git merge*),Bash(gh pr merge*)"

# -------------------------------------------------------- wrap-up helpers
#
# Byte-identical to run.sh's own scan_log_clean/post_telegram_summary —
# duplicated rather than sourced for the same reason the lock logic is: no
# shared file exists to source them from without inventing one just for two
# callers, and run.sh explicitly stops before spawning claude/gitleaks/
# Telegram when sourced for tests, so these helpers are not currently
# importable as functions anyway.
scan_log_clean() {
  local log_file="$1" out_file="$2"
  if ! command -v gitleaks >/dev/null 2>&1; then
    note "gitleaks not on PATH — cannot scan the run log, so nothing is posted (fix: brew install gitleaks)"
    return 2
  fi
  local scan_dir
  scan_dir="$(mktemp -d)"
  cp "$log_file" "$scan_dir/"
  if gitleaks dir "$scan_dir" --no-banner --redact >"$out_file" 2>&1; then
    rm -rf "$scan_dir"
    return 0
  fi
  rm -rf "$scan_dir"
  return 1
}

post_telegram_summary() {
  local gatekeeper_scripts="$1" text="$2"
  if [[ -z "${GATEKEEPER_TG_TOKEN:-}" || -z "${GATEKEEPER_TG_CHAT_ID:-}" ]]; then
    note "GATEKEEPER_TG_TOKEN/GATEKEEPER_TG_CHAT_ID not set — skipping the Telegram summary"
    return 0
  fi
  GATEKEEPER_TG_TOKEN="$GATEKEEPER_TG_TOKEN" GATEKEEPER_TG_CHAT_ID="$GATEKEEPER_TG_CHAT_ID" \
    python3 - "$gatekeeper_scripts" "$text" <<'PY'
import os
import sys

sys.path.insert(0, sys.argv[1])
from tg import TelegramError, TelegramTransport, send_text  # noqa: E402

try:
    send_text(
        TelegramTransport(os.environ["GATEKEEPER_TG_TOKEN"]),
        os.environ["GATEKEEPER_TG_CHAT_ID"],
        sys.argv[2],
    )
except TelegramError as e:
    print(f"triage-run: telegram summary post failed: {e}", file=sys.stderr)
PY
}

# Sourced-for-tests early exit, same shape as run.sh's own — nothing below
# this line runs under `.github/scripts/check-*.sh` sourcing this file for
# its lock/helper functions.
[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0

for b in python3 jq claude; do
  command -v "$b" >/dev/null 2>&1 || die "$b not found on PATH"
done

GATEKEEPER_SCRIPTS="$(sibling_scripts gatekeeper gate.py)"

mkdir -p "$RUN_LOG_DIR"

INVOCATION_ID="$(gen_uuid)"
LOG_FILE="$RUN_LOG_DIR/triage-$INVOCATION_ID.log"

exec > >(tee -a "$LOG_FILE") 2>&1
note "invocation $INVOCATION_ID starting"

acquire_lock

# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"
# shellcheck source=/dev/null
source "$GATEKEEPER_SCRIPTS/env.sh"
[[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] \
  || die "CLAUDE_CODE_OAUTH_TOKEN is not set — source scripts/env.sh (op://agents/cycle-runner-claude/token) or export it"

session_id="$(gen_uuid)"
run_id="$INVOCATION_ID"
note "starting /triage (session $session_id, run $run_id)"

summary_line="triage run FAILED — needs a human"
if claude_out="$(claude --session-id "$session_id" -p "/triage --session-id $session_id --run-id $run_id" \
    --output-format json --json-schema "$JSON_SCHEMA" \
    --permission-mode acceptEdits \
    --allowedTools "$ALLOWED_TOOLS" --disallowedTools "$DISALLOWED_TOOLS" 2>&1)"; then
  note "/triage: $claude_out"
  status="$(jq -r '.status // "?"' <<<"$claude_out" 2>/dev/null || echo '?')"
  summary_line="/triage started (session $session_id) — status $status"
else
  note "/triage FAILED: $claude_out"
  summary_line="/triage invocation FAILED — needs a human"
fi

summary="triage-run $(date -u +%Y-%m-%dT%H:%M:%SZ) (run $INVOCATION_ID)
• $summary_line"

scan_rc=0
scan_log_clean "$LOG_FILE" "$RUN_LOG_DIR/triage-$INVOCATION_ID.gitleaks.out" || scan_rc=$?
case "$scan_rc" in
  0)
    post_telegram_summary "$GATEKEEPER_SCRIPTS" "$summary"
    ;;
  2)
    note "run-log scan could not run — NOT posting to Telegram; log kept at $LOG_FILE for a human to review"
    ;;
  *)
    note "gitleaks flagged something in the run log — NOT posting to Telegram (see $RUN_LOG_DIR/triage-$INVOCATION_ID.gitleaks.out); log kept at $LOG_FILE for a human to review"
    ;;
esac

note "invocation $INVOCATION_ID done"
