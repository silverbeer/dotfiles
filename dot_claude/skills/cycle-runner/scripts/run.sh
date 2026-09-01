#!/usr/bin/env bash
# cycle-runner/scripts/run.sh — SB-929. ONE entry point, ONE unit of work, exit.
#
# Invoked by launchd (SB-930, not built yet) on a timer. It never loops —
# that is what makes it safe to run on a schedule. Each invocation:
#
#   1. Acquires a lock (mkdir-atomic, pid-staleness checked). Contended by a
#      still-running previous invocation -> exit 0 quietly.
#   2. Drains `gate.py poll --once`. For every gate that resolved:
#      - approved `merge`  -> THIS SCRIPT re-confirms CI is green (never trusts
#        the gate answer as a substitute for that check) and runs
#        `gh pr merge --squash --delete-branch` itself — never Claude
#        (work-headless.md's phase 9 never runs `gh pr merge`; its
#        `--disallowedTools` excludes it for exactly this reason).
#      - approved, any other kind -> `claude -p --resume <session_id> "approved: <note>"`
#      - rejected                 -> `claude -p --resume <session_id> "rejected: <reason>"`
#      - needs-human              -> nothing further; already flagged
#   3. If nothing resolved to act on, picks ONE ready ticket via pick.py and
#      starts it: `claude --session-id S -p "/work-headless SB-N --session-id S
#      --run-id R" ...`.
#   4. Releases the lock, writes a run log, gitleaks-scans it (skipped with a
#      note if gitleaks is not on PATH — never blocks on a missing binary),
#      and posts a short summary to Telegram via gatekeeper's own tg.py
#      transport (reused, never reimplemented).
#
# `--max-turns` is NOT passed to `claude -p` below: `claude --help` on the
# version this was built against (2.1.251) has no such flag. The ticket asked
# for real flag names, not a guess — this is the one requested item this
# script could not carry, and it is flagged in the delivery report, not
# silently dropped.
# shellcheck disable=SC2016  # ci_state()'s jq filter quotes its own $s literally, on purpose
set -euo pipefail
export NO_COLOR=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Same root gate.py's own state_dir() defaults to, and the same env var — lock,
# runs/ (RUN_SCRATCH, written by work-headless.md) and logs/ all live under it.
STATE="${GATEKEEPER_STATE:-$HOME/.local/state/cycle-runner}"
RUN_LOG_DIR="$STATE/logs"
LOCK_DIR="$STATE/lock"

die() { echo "cycle-runner: $*" >&2; exit 1; }
note() { echo "cycle-runner: $*"; }

# A peer skill's scripts dir, resolved the same way linear.sh's sibling() and
# gate.py's _linear_scripts() do: try the chezmoi source tree layout, then the
# deployed ~/.claude one, and take whichever actually has the marker file.
sibling_scripts() {
  local skill="$1" marker="$2" f
  for f in "$SCRIPT_DIR/../../$skill/scripts/$marker" "$HOME/.claude/skills/$skill/scripts/$marker"; do
    [[ -f "$f" ]] && { dirname "$f"; return 0; }
  done
  die "$skill/scripts/$marker not found — the $skill skill must be installed"
}

# ------------------------------------------------- message helpers (SB-945)
# The Telegram summary is read on a phone by someone deciding whether they
# need to act. Every line that names a ticket therefore carries its title and
# a link, and no line carries a UUID — session and run ids are in the run log,
# which is where anyone debugging already is.

# A newline-delimited "KEY<tab>title" memo, NOT an associative array: the plist
# runs this with /bin/bash, which on macOS is 3.2 and has no `declare -A`. That
# would have failed on every tick, on the one machine this is deployed to.
TICKET_TITLES=""

# Resolved non-fatally: a machine without linear-crud still gets messages, just
# without titles. A missing title must never be the reason a tick dies.
LINEAR_SCRIPTS="$(sibling_scripts linear-crud linear.sh 2>/dev/null || true)"

ticket_title() {  # KEY — the issue title, memoised per invocation
  local key="$1" cached t=""
  cached="$(printf '%s\n' "$TICKET_TITLES" | awk -F'\t' -v k="$key" '$1 == k { print $2; exit }')"
  if [[ -n "$cached" ]]; then
    printf '%s' "$cached"
    return 0
  fi
  # </dev/null for the same reason as everywhere else here: this can be called
  # from inside a `while read` loop.
  if [[ -n "$LINEAR_SCRIPTS" ]]; then
    t="$(bash "$LINEAR_SCRIPTS/linear.sh" view "$key" </dev/null 2>/dev/null | tail -1 | jq -r '.title // empty' 2>/dev/null)"
  fi
  t="${t:-$key}"
  # Titles run long and several carry their own em-dash, which fights the one
  # in the headline. 64 chars keeps a headline on one or two phone lines.
  if [[ "${#t}" -gt 64 ]]; then
    t="${t:0:63}…"
  fi
  TICKET_TITLES="$TICKET_TITLES
$key	$t"
  printf '%s' "$t"
}

ticket_link() {  # KEY — resolves on the bare identifier, no slug needed
  printf 'https://linear.app/silverbeer/issue/%s' "$1"
}

# One summary entry: a headline naming the ticket and what happened, a sentence
# saying what happens next, and a link. Blank line between entries so several
# stay readable in one message.
# What happens next, phrased for someone reading on a phone who wants to know
# whether they are on the hook. `awaiting` is the common case and MUST say so —
# it is the whole reason the message exists.
started_next_line() {  # STATUS
  case "$1" in
    awaiting) printf "Planning it now. I'll ask before making any changes." ;;
    blocked)  printf "I could not get started on it — see the ticket for why." ;;
    *)        printf "Run finished as '%s'." "$1" ;;
  esac
}

resume_next_line() {  # STATUS DECISION
  case "$1" in
    awaiting) printf "Picked it back up after you %s it. I'll come back when I need you." "${2%%:*}" ;;
    blocked)  printf "Picked it back up but hit a blocker — see the ticket." ;;
    *)        printf "Picked it back up; the run finished as '%s'." "$1" ;;
  esac
}

say() {  # HEADLINE NEXT LINK
  SUMMARY_LINES+=("$1
$2
$3
")
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
# mkdir is atomic on every POSIX filesystem, so "did I just create the
# directory" is a safe test-and-set with no separate lock file needed. The pid
# file inside it is only for staleness: a lock left behind by a process that
# is no longer running (crash, `kill -9`, a reboot) must not wedge every
# future tick forever.

release_lock() { rm -rf "$LOCK_DIR"; }

# mkdir and the pid write are two separate steps, not one atomic operation: a
# holder that just mkdir'd but hasn't reached `echo $$ >pid` yet leaves a
# window where the lock dir exists with no pid file at all. Reading that as
# "stale" would steal a lock someone else is mid-way through acquiring — a
# real race, not theoretical, since launchd can in principle fire two ticks
# close together. An empty/missing pid file is only treated as abandoned once
# a "created_at" timestamp file (written right after mkdir, before pid) is
# older than this grace period. A plain epoch number in a file we control
# avoids stat(1)'s BSD-vs-GNU flag incompatibility (-f %m vs -c %Y) entirely —
# a launchd tick every 30 min makes a few seconds of grace free.
LOCK_GRACE_SECS=10

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    date +%s >"$LOCK_DIR/created_at"
    echo $$ >"$LOCK_DIR/pid"
    trap release_lock EXIT  # only once we actually own it — never on a lock we lost the race for
    return 0
  fi
  local pid
  pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    note "lock held by running pid $pid — another invocation is in progress, exiting quietly"
    exit 0
  fi
  if [[ -z "$pid" ]]; then
    local created now age
    # Missing created_at (a holder between mkdir and the created_at write, the
    # very first instant of the race) is treated as age 0 — the freshest
    # possible state, not a special case — so it still respects
    # LOCK_GRACE_SECS=0 (a deliberate "treat as stale right now" override)
    # the same way an aged timestamp would.
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
  # Lost the race to a concurrent invocation also clearing the stale lock.
  note "lost the race for the lock after clearing a stale one — exiting quietly"
  exit 0
}

# ----------------------------------------------------------------- claude -p

# work-headless.md's own output contract (its "Rules that survive every
# phase" section). Passed on every claude -p / --resume call so a malformed
# turn is caught at the harness level, not read off free text.
JSON_SCHEMA='{"type":"object","properties":{"status":{"type":"string","enum":["awaiting","blocked","error"]},"ticket":{"type":["string","null"]},"branch":{"type":["string","null"]},"pr_url":{"type":["string","null"]},"session_id":{"type":["string","null"]}},"required":["status","ticket","branch","pr_url","session_id"]}'

ALLOWED_TOOLS="Bash,Read,Edit,Write,Grep,Glob,Agent"
# Merge happens in THIS script, on an approved gate — never inside claude -p.
# force-push and a plain `git merge` are excluded for the same "no
# self-authorized escape hatch" reason (docs/agentic-delivery.md "Guard rails").
DISALLOWED_TOOLS="Bash(git push --force*),Bash(git merge*),Bash(gh pr merge*)"

SUMMARY_LINES=()

resume_session() {
  local ticket="$1" session_id="$2" message="$3"
  if [[ -z "$session_id" || "$session_id" == "null" ]]; then
    note "gate for $ticket resolved but recorded no session_id — cannot resume"
    say "$ticket needs you — $(ticket_title "$ticket")" \
        "Your decision was recorded but the run it belongs to was not, so I cannot pick it back up. Re-run it from scratch when you want it." \
        "$(ticket_link "$ticket")"
    return 0
  fi
  note "resuming $ticket session $session_id: $message"
  local out
  # </dev/null is load-bearing (SB-952): this runs inside `while read` over a
  # process substitution, and `claude` reads stdin. Without it the first resume
  # swallows every remaining line and the loop handles exactly ONE gate per
  # tick, however many resolved — silently dropping the others' approvals.
  if out="$(claude -p --resume "$session_id" "$message" \
      --output-format json --json-schema "$JSON_SCHEMA" \
      --permission-mode acceptEdits \
      --allowedTools "$ALLOWED_TOOLS" --disallowedTools "$DISALLOWED_TOOLS" \
      </dev/null 2>&1)"; then
    note "resume for $ticket: $out"
    local st
    st="$(jq -r '.structured_output.status // "unknown"' <<<"$out" 2>/dev/null || echo unknown)"
    say "Resumed $ticket — $(ticket_title "$ticket")" \
        "$(resume_next_line "$st" "$message")" \
        "$(ticket_link "$ticket")"
  else
    note "resume for $ticket FAILED: $out"
    say "$ticket needs you — $(ticket_title "$ticket")" \
        "I could not resume the run after your decision. It will need starting again." \
        "$(ticket_link "$ticket")"
  fi
}

# Same `gh pr checks` reduction work-headless.md phase 9 uses, called again
# here rather than trusted from the gate: CI can go red in the time a merge
# gate sat open waiting for a human tap.
#
# `state` on a `gh pr checks --json state` row is either a StatusContext state
# (SUCCESS/PENDING/ERROR/FAILURE/EXPECTED) or, for a CheckRun, its GraphQL
# status while running (QUEUED/IN_PROGRESS/WAITING/REQUESTED/PENDING) or its
# conclusion once COMPLETED (SUCCESS/FAILURE/NEUTRAL/CANCELLED/SKIPPED/
# TIMED_OUT/ACTION_REQUIRED/STALE/STARTUP_FAILURE) — `gh help pr checks` lists
# the JSON fields but not this vocabulary; confirmed from the `gh` binary's own
# embedded strings and the checks.go state-derivation logic. An explicit
# allowlist, not "not FAILURE/PENDING": only SUCCESS/NEUTRAL/SKIPPED pass;
# PENDING/IN_PROGRESS/QUEUED are still running; everything else — every other
# named state (CANCELLED, ERROR, TIMED_OUT, ACTION_REQUIRED, STARTUP_FAILURE,
# STALE, EXPECTED, WAITING, REQUESTED) and anything not on this list at all —
# counts as failure. A merge gate must never fail open on an unrecognized
# state.
ci_state() {
  local pr_url="$1"
  gh pr checks "$pr_url" --json state \
    --jq '[.[].state] | if any(. as $s | ($s | IN("SUCCESS","NEUTRAL","SKIPPED","PENDING","IN_PROGRESS","QUEUED") | not)) then "failure"
           elif any(.=="PENDING" or .=="IN_PROGRESS" or .=="QUEUED") then "pending"
           else "success" end' \
    2>/dev/null || echo pending
}

handle_merge_gate() {
  local ticket="$1" run_id="$2" gate_id="$3"
  local pr_file="$STATE/runs/$run_id/pr_url"
  if [[ ! -f "$pr_file" ]]; then
    note "merge gate $gate_id ($ticket): no $pr_file — cannot find the PR, not merging"
    say "$ticket needs you — $(ticket_title "$ticket")" \
        "You approved the merge but I lost track of which PR it was. Merge it by hand, or re-run the ticket." \
        "$(ticket_link "$ticket")"
    return 0
  fi
  local pr_url state
  pr_url="$(cat "$pr_file")"
  state="$(ci_state "$pr_url")"
  if [[ "$state" != "success" ]]; then
    note "merge gate $gate_id ($ticket): CI is '$state' on re-check — approval is stale, not merging"
    say "$ticket not merged — $(ticket_title "$ticket")" \
        "You approved it, but CI is now '$state', so I stopped. Nothing was merged." \
        "$pr_url"
    return 0
  fi
  # </dev/null for the same reason as resume_session (SB-952) — gh reads stdin
  # and this is called from inside the same `while read` loop.
  if gh pr merge "$pr_url" --squash --delete-branch </dev/null; then
    note "merged $pr_url for $ticket — 'Fixes $ticket' in the PR body moves Linear to Done automatically"
    say "Merged $ticket — $(ticket_title "$ticket")" \
        "CI was green. Linear closes the ticket automatically." \
        "$pr_url"
  else
    note "gh pr merge FAILED for $ticket ($pr_url)"
    say "$ticket needs you — $(ticket_title "$ticket")" \
        "CI was green and you approved, but the merge itself failed. Worth merging by hand." \
        "$pr_url"
  fi
}

# Extracted so the "every resolved gate is handled" contract is testable
# (SB-952) — the loop used to live inline, below the sourcing guard, where no
# test could reach it.
drain_resolved_gates() {  # RESOLVED_JSON on stdin as TSV lines
  local gate_id ticket status detail kind session_id run_id note_text
  while IFS=$'\t' read -r gate_id ticket status; do
    [[ -z "$gate_id" ]] && continue
    acted=1
    detail="$(python3 "$GATE_PY" status "$gate_id")"
    kind="$(jq -r '.kind' <<<"$detail")"
    session_id="$(jq -r '.session_id' <<<"$detail")"
    run_id="$(jq -r '.run_id' <<<"$detail")"
    note_text="$(jq -r '.note // ""' <<<"$detail")"

    if [[ "$status" == "approved" && "$kind" == "merge" ]]; then
      handle_merge_gate "$ticket" "$run_id" "$gate_id"
    elif [[ "$status" == "approved" ]]; then
      resume_session "$ticket" "$session_id" "approved: $note_text"
    elif [[ "$status" == "rejected" ]]; then
      # work-headless.md has no "resume-on-reject" phase defined yet — this
      # resumes it anyway and lets that turn fail/report naturally, per the
      # ticket's own instruction. Flagged again in the delivery report.
      resume_session "$ticket" "$session_id" "rejected: $note_text"
    else
      note "gate $gate_id ($ticket, $kind) resolved as '$status' — no runner action, already flagged"
      say "$ticket is waiting on you — $(ticket_title "$ticket")" \
          "The $kind gate came back as $status, which I cannot act on myself." \
          "$(ticket_link "$ticket")"
    fi
  done
}

# -------------------------------------------------------- wrap-up helpers

# gitleaks scans a DIRECTORY (`gitleaks dir`, same as check-gitleaks.sh), so
# the run log is copied alone into a throwaway dir rather than pointed at
# $RUN_LOG_DIR — scanning every historical log on every tick would mean one
# old, already-seen finding blocks every summary forever after.
scan_log_clean() {
  local log_file="$1" out_file="$2"
  # Fails CLOSED (SB-943). "Never block on a missing binary" is right for a
  # linter and wrong for the only thing standing between a run log — which
  # contains verbatim `claude -p` output — and an outbound channel. A missing
  # scanner means the log stays on this machine.
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
  # An idle tick has nothing to say, so it says nothing (SB-945).
  if [[ -z "${text//[[:space:]]/}" ]]; then
    note "nothing happened this tick — no Telegram summary sent"
    return 0
  fi
  if [[ -z "${GATEKEEPER_TG_TOKEN:-}" || -z "${GATEKEEPER_TG_CHAT_ID:-}" ]]; then
    note "GATEKEEPER_TG_TOKEN/GATEKEEPER_TG_CHAT_ID not set — skipping the Telegram summary"
    return 0
  fi
  # tg.py's TelegramTransport + send_text, imported and called directly — no
  # second implementation of the Bot API call.
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
    print(f"cycle-runner: telegram summary post failed: {e}", file=sys.stderr)
PY
}

# Sourced by .github/scripts/check-cycle-runner-policy.sh for lock/ci_state/
# merge unit tests: stop here so the functions above are defined, and nothing
# below — which would spawn `claude`, hit Telegram, or shell out to `gh pr
# merge` for real — ever runs.
[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0

for b in python3 jq gh claude; do
  command -v "$b" >/dev/null 2>&1 || die "$b not found on PATH"
done

GATEKEEPER_SCRIPTS="$(sibling_scripts gatekeeper gate.py)"
GATE_PY="$GATEKEEPER_SCRIPTS/gate.py"
PICK_PY="$SCRIPT_DIR/pick.py"

mkdir -p "$RUN_LOG_DIR"

# One id per INVOCATION (not per ticket): when step 3 picks a brand-new ticket
# this id doubles as that ticket's first run_id — fine, since "one run_id per
# ticket lifecycle" (work-headless.md) just means it does not change on
# resume, and a ticket's very first run_id has to come from somewhere. When
# step 2 instead resumes/merges existing tickets, this id is purely the log
# filename for what THIS tick did; it never enters gate state.
INVOCATION_ID="$(gen_uuid)"
LOG_FILE="$RUN_LOG_DIR/$INVOCATION_ID.log"

# Everything printed from here on — including from sourced env.sh, gate.py,
# and claude itself — lands in the run log AND still reaches launchd's own
# stdout/stderr capture. gitleaks scans $LOG_FILE specifically, at the end,
# before the one place any of this could leave the machine (Telegram).
exec > >(tee -a "$LOG_FILE") 2>&1
note "invocation $INVOCATION_ID starting"

acquire_lock

# CLAUDE_CODE_OAUTH_TOKEN from 1Password if unset (never printed); the
# gatekeeper's own Telegram vars the same way, since the wrap-up summary below
# reuses gate.py's transport and needs the same two vars gate.py does.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/env.sh"
# shellcheck source=/dev/null
source "$GATEKEEPER_SCRIPTS/env.sh"
[[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] \
  || die "CLAUDE_CODE_OAUTH_TOKEN is not set — source scripts/env.sh (op://agents/cycle-runner-claude/token) or export it"

# ---------------------------------------------------------- 2. drain gates

RESOLVED_JSON="$(python3 "$GATE_PY" poll --once)"
resolved_count="$(jq '.resolved | length' <<<"$RESOLVED_JSON")"
acted=0


if [[ "$resolved_count" -gt 0 ]]; then
  drain_resolved_gates \
    < <(jq -r '.resolved[] | [.gate_id, .ticket, .status] | @tsv' <<<"$RESOLVED_JSON")
fi

# ------------------------------------------------------- 3. pick, if idle

if [[ "$acted" -eq 0 ]]; then
  ticket="$(python3 "$PICK_PY")"
  if [[ -z "$ticket" ]]; then
    note "nothing resolved, no ready ticket — nothing to do this tick"
    # An idle tick normally posts nothing (SB-945) — silence means idle. But
    # silence must not also mean "everything is parked waiting on you", which
    # is indistinguishable and was (SB-949). Say so once a day, not every tick.
    parked="$(jq -r '[.parked[]? | select(.hours >= 12)] | length' <<<"$RESOLVED_JSON" 2>/dev/null || echo 0)"
    if [[ "${parked:-0}" -gt 0 ]]; then
      say "$parked ticket(s) waiting on you" \
          "$(jq -r '[.parked[] | select(.hours >= 12) | "\(.ticket) (\(.kind), \(.hours)h)"] | join(", ")' <<<"$RESOLVED_JSON")" \
          "https://linear.app/silverbeer/team/SB/active"
    fi
  else
    session_id="$(gen_uuid)"
    run_id="$INVOCATION_ID"
    note "picked $ticket — starting /work-headless (session $session_id, run $run_id)"
    prompt="/work-headless $ticket --session-id $session_id --run-id $run_id"
    if claude_out="$(claude --session-id "$session_id" -p "$prompt" \
        --output-format json --json-schema "$JSON_SCHEMA" \
        --permission-mode acceptEdits \
        --allowedTools "$ALLOWED_TOOLS" --disallowedTools "$DISALLOWED_TOOLS" 2>&1)"; then
      note "work-headless for $ticket: $claude_out"
      started_st="$(jq -r '.structured_output.status // "unknown"' <<<"$claude_out" 2>/dev/null || echo unknown)"
      say "Started $ticket — $(ticket_title "$ticket")" \
          "$(started_next_line "$started_st")" \
          "$(ticket_link "$ticket")"
    else
      note "work-headless for $ticket FAILED: $claude_out"
      say "$ticket needs you — $(ticket_title "$ticket")" \
          "I could not start the run at all — that is usually a setup problem on the mini, not the ticket." \
          "$(ticket_link "$ticket")"
    fi
  fi
fi

# --------------------------------------------------------------- 4. wrap-up

# No run id, no timestamp, no bullet scaffolding: the entries say what
# happened and link where to act. The run log keeps the ids for debugging.
summary="$(printf '%s\n' "${SUMMARY_LINES[@]+"${SUMMARY_LINES[@]}"}")"

scan_rc=0
scan_log_clean "$LOG_FILE" "$RUN_LOG_DIR/$INVOCATION_ID.gitleaks.out" || scan_rc=$?
case "$scan_rc" in
  0)
    post_telegram_summary "$GATEKEEPER_SCRIPTS" "$summary"
    ;;
  2)
    note "run-log scan could not run — NOT posting to Telegram; log kept at $LOG_FILE for a human to review"
    ;;
  *)
    note "gitleaks flagged something in the run log — NOT posting to Telegram (see $RUN_LOG_DIR/$INVOCATION_ID.gitleaks.out); log kept at $LOG_FILE for a human to review"
    ;;
esac

note "invocation $INVOCATION_ID done"
