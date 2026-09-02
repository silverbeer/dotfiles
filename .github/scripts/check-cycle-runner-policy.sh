#!/usr/bin/env bash
# Offline tests for the cycle-runner skill (SB-929): pick.py's driven x
# estimate x label autonomy policy and ready-queue blocker detection (pure
# python, no network — dot_claude/skills/cycle-runner/tests/), and run.sh's
# bash-level mechanics (the CI
# green/pending/failure reduction, and the merge-gate decision) sourced
# directly with `gh` stubbed — never a real `claude`, `gh`, or Telegram call.
#
# run.sh is never executed for real here: it carries its own source guard
# (`[[ "${BASH_SOURCE[0]}" == "$0" ]] || return 0`, same pattern linear.sh
# uses) so `source`-ing it defines the functions below without running the
# invocation body that would spawn `claude -p`, hit Telegram, or shell out to
# `gh pr merge`.
#
# Marker-comment parsing (the `<!-- sb-agent:{kind}:{run_id}:{session_id} -->`
# format) is NOT tested here — pick.py and run.sh never parse or emit it;
# that is entirely gate.py's job and is already covered by
# check-gatekeeper.sh's test_gate.py.
# shellcheck disable=SC2016  # the run_sourced/sed snippets quote run.sh's $-names literally, on purpose
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

for b in python3 bash; do
  command -v "$b" >/dev/null 2>&1 || die "$b is not installed"
done

RUN_SH="$REPO/dot_claude/skills/cycle-runner/scripts/run.sh"
PICK_PY="$REPO/dot_claude/skills/cycle-runner/scripts/pick.py"
TESTS_DIR="$REPO/dot_claude/skills/cycle-runner/tests"
[ -f "$RUN_SH" ] || die "missing $RUN_SH — wrong REPO?"
[ -f "$PICK_PY" ] || die "missing $PICK_PY — wrong REPO?"
[ -d "$TESTS_DIR" ] || die "missing $TESTS_DIR — wrong REPO?"

fails=0
ok()  { note "ok   $*"; }
bad() { err "$*"; fails=$((fails + 1)); }

# ----------------------------------------- 1. pick.py policy — python unit tests

note "1. pick.py policy + ready-queue unit tests in $TESTS_DIR"
export PYTHONDONTWRITEBYTECODE=1  # never leave __pycache__ in the source tree
if py_out="$(cd "$REPO" && python3 -m unittest discover -s "$TESTS_DIR" -p 'test_*.py' -v 2>&1)"; then
  ok "python: $(printf '%s\n' "$py_out" | grep -E '^(Ran|OK)' | tr '\n' ' ')"
else
  bad "pick.py unit tests failed:"
  printf '%s\n' "$py_out" | sed 's/^/    | /' >&2
fi
py_ran="$(printf '%s\n' "$py_out" | sed -nE 's/^Ran ([0-9]+) tests?.*/\1/p')"
[ "${py_ran:-0}" -ge 20 ] || bad "python: expected at least 20 tests to run, unittest reported '${py_ran:-none}' — discovery broken?"

# ------------------------------------------------- 2. run.sh bash mechanics

note "2. run.sh CI-state / merge-gate mechanics (sourced, gh stubbed)"

STATE_DIR="$WORK/state"
bin="$WORK/bin"; mkdir -p "$bin"

# `gh` is replaced per-scenario below; a default stub that fails loudly means
# any call this suite did not anticipate is a test failure, not a silent
# no-op.
default_gh() {
  cat >"$bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "check-cycle-runner-policy: unexpected gh call: $*" >&2
exit 97
STUB
  chmod +x "$bin/gh"
}
default_gh
export PATH="$bin:$PATH"

# Source run.sh in a fresh subshell per scenario, exactly like harness.sh's
# per-test subshell: one scenario's stubs, traps and state must not leak into
# the next.
run_sourced() {  # CODE — bash run inside a subshell with run.sh sourced first
  bash -c '
    set -uo pipefail
    export GATEKEEPER_STATE="$1"; shift
    # shellcheck disable=SC1090
    source "$1"; shift
    eval "$1"
  ' _ "$STATE_DIR" "$RUN_SH" "$1"
}

# --- 2a. concurrency is the scheduler's now (SB-976)
#
# The lock scenarios that used to live here — acquired, contended, stale pid,
# and the mkdir/pid race window — are gone with the code they tested.
# `concurrencyPolicy: Forbid` on the CronJob is the guarantee, enforced by the
# thing that decides when a tick starts, so there is nothing in run.sh left to
# unit-test. What replaces them is an assertion that the lock did not quietly
# come back: a second mechanism alongside Forbid is the two-sources-of-truth
# shape behind SB-949 and SB-952, and it would be easy to reintroduce "just
# for local runs".

for sym in acquire_lock release_lock LOCK_DIR LOCK_GRACE_SECS; do
  if grep -qE "(^|[^[:alnum:]_])$sym([^[:alnum:]_]|$)" "$RUN_SH"; then
    bad "run.sh mentions $sym — the hand-rolled lock is back alongside concurrencyPolicy: Forbid (SB-976)"
  fi
done
ok "no hand-rolled lock in run.sh — concurrency is concurrencyPolicy: Forbid"

# --- 2d. ci_state: success / pending / failure

gh_checks_stub() {  # STATE — makes `gh pr checks … --json state --jq …` report it
  cat >"$bin/gh" <<STUB
#!/usr/bin/env bash
if [[ "\$1 \$2" == "pr checks" ]]; then echo "$1"; exit 0; fi
echo "check-cycle-runner-policy: unexpected gh call: \$*" >&2
exit 97
STUB
  chmod +x "$bin/gh"
}
# The stub above hands back the STATE VALUE directly (ci_state's --jq reduces
# gh's real JSON to exactly one of success/pending/failure) — the point under
# test is ci_state()'s own control flow, not gh's --jq expression.
ci_state_case() {
  local want="$1"
  gh_checks_stub "$want"
  local got
  got="$(run_sourced 'ci_state "https://example.invalid/pr/1"')"
  if [ "$got" = "$want" ]; then ok "ci_state: gh reports $want -> $got"
  else bad "ci_state: gh reports $want, ci_state() returned '$got'"; fi
}
ci_state_case success
ci_state_case pending
ci_state_case failure

# --- 2d2. ci_state: the real jq filter, exercised over the actual gh check
# state vocabulary (SB-929 review fix) — 2d above bypasses ci_state()'s own
# `--jq` expression entirely (its stub hands back the wanted word directly,
# by design, per the comment above it), so a broken filter could pass 2d and
# still fail open on, say, a CANCELLED check in reality. This stub instead
# returns real `[{"state": ...}, ...]` JSON and pipes it through the actual
# filter string embedded in run.sh via the real `jq` binary — `gh`'s own
# `--jq` flag is documented to behave like `jq -r` for a scalar result, which
# is what run.sh's `[[ "$state" != … ]]` string comparisons assume.
states_json() {  # STATE...
  local json="[" first=1 s
  for s in "$@"; do
    [ "$first" -eq 1 ] && first=0 || json+=","
    json+="{\"state\":\"$s\"}"
  done
  printf '%s]' "$json"
}
gh_checks_real_jq_stub() {  # STATE...
  local json; json="$(states_json "$@")"
  cat >"$bin/gh" <<STUB
#!/usr/bin/env bash
if [[ "\$1 \$2" == "pr checks" ]]; then
  filter="\${*: -1}"
  printf '%s' '$json' | jq -r "\$filter"
  exit 0
fi
echo "check-cycle-runner-policy: unexpected gh call: \$*" >&2
exit 97
STUB
  chmod +x "$bin/gh"
}
ci_state_real_jq_case() {  # WANT STATE...
  local want="$1"; shift
  gh_checks_real_jq_stub "$@"
  local got
  got="$(run_sourced 'ci_state "https://example.invalid/pr/1"')"
  if [ "$got" = "$want" ]; then ok "ci_state (real jq): states=($*) -> $want"
  else bad "ci_state (real jq): states=($*) expected $want, got '$got'"; fi
}
# Passing set: only SUCCESS/NEUTRAL/SKIPPED.
ci_state_real_jq_case success SUCCESS
ci_state_real_jq_case success SUCCESS NEUTRAL SKIPPED
# Still running: PENDING/IN_PROGRESS/QUEUED.
ci_state_real_jq_case pending SUCCESS PENDING
ci_state_real_jq_case pending SUCCESS IN_PROGRESS
ci_state_real_jq_case pending SUCCESS QUEUED
# NEGATIVE — everything that isn't literally FAILURE/PENDING used to fall
# through to "success" before this fix; each of these must be "failure".
ci_state_real_jq_case failure SUCCESS CANCELLED
ci_state_real_jq_case failure SUCCESS ERROR
ci_state_real_jq_case failure SUCCESS TIMED_OUT
ci_state_real_jq_case failure SUCCESS ACTION_REQUIRED
ci_state_real_jq_case failure SUCCESS STARTUP_FAILURE
# NEGATIVE — an unrecognized state must never be silently read as success.
ci_state_real_jq_case failure SUCCESS SOME_FUTURE_STATE_GH_ADDS_LATER
# A known failure state alongside a still-pending one is "failure", not
# "pending" — failure takes priority, same as the pre-fix reduction did.
ci_state_real_jq_case failure PENDING CANCELLED

default_gh  # restore the loud default before the merge-gate scenarios below

# --- 2e. handle_merge_gate: missing pr_url file -> no merge attempted

rm -rf "$STATE_DIR"; mkdir -p "$STATE_DIR"
default_gh
if out="$(run_sourced 'handle_merge_gate SB-1 run-without-pr gate-1; printf "%s\n" "${SUMMARY_LINES[@]}"')"; then
  if [[ "$out" == *"lost track of which PR"* ]]; then ok "handle_merge_gate: no pr_url file -> not merged, summary says so"
  else bad "handle_merge_gate: missing pr_url case did not report correctly: $out"; fi
else
  bad "handle_merge_gate: missing pr_url case exited non-zero: $out"
fi

# --- 2f. handle_merge_gate: CI green -> gh pr merge is actually called

rm -rf "$STATE_DIR"; mkdir -p "$STATE_DIR/runs/run-green"
echo "https://github.com/silverbeer/dotfiles/pull/1" >"$STATE_DIR/runs/run-green/pr_url"
MERGE_MARKER="$WORK/merge-was-called"; rm -f "$MERGE_MARKER"
cat >"$bin/gh" <<STUB
#!/usr/bin/env bash
if [[ "\$1 \$2" == "pr checks" ]]; then echo success; exit 0; fi
if [[ "\$1 \$2" == "pr merge" ]]; then echo "\$*" >"$MERGE_MARKER"; exit 0; fi
echo "check-cycle-runner-policy: unexpected gh call: \$*" >&2
exit 97
STUB
chmod +x "$bin/gh"
out="$(run_sourced 'handle_merge_gate SB-2 run-green gate-2; printf "%s\n" "${SUMMARY_LINES[@]}"')"
if [ -f "$MERGE_MARKER" ] && grep -q -- '--squash' "$MERGE_MARKER" && grep -q -- '--delete-branch' "$MERGE_MARKER"; then
  ok "handle_merge_gate: CI green -> gh pr merge --squash --delete-branch called"
else
  bad "handle_merge_gate: CI green did not call gh pr merge as expected: $(cat "$MERGE_MARKER" 2>/dev/null || echo '<not called>')"
fi
if [[ "$out" == *"Merged SB-2"* ]] && [[ "$out" == *"https://github.com/silverbeer/dotfiles/pull/1"* ]]; then
  ok "handle_merge_gate: summary names the ticket and the merged PR"
else
  bad "handle_merge_gate: summary did not name the merged PR: $out"
fi

# --- 2g. handle_merge_gate: approval is stale (CI now failing) -> NOT merged

rm -rf "$STATE_DIR"; mkdir -p "$STATE_DIR/runs/run-red"
echo "https://github.com/silverbeer/dotfiles/pull/2" >"$STATE_DIR/runs/run-red/pr_url"
rm -f "$MERGE_MARKER"
cat >"$bin/gh" <<STUB
#!/usr/bin/env bash
if [[ "\$1 \$2" == "pr checks" ]]; then echo failure; exit 0; fi
if [[ "\$1 \$2" == "pr merge" ]]; then echo "\$*" >"$MERGE_MARKER"; exit 0; fi
exit 97
STUB
chmod +x "$bin/gh"
out="$(run_sourced 'handle_merge_gate SB-3 run-red gate-3; printf "%s\n" "${SUMMARY_LINES[@]}"')"
if [ ! -f "$MERGE_MARKER" ] && [[ "$out" == *"Nothing was merged"* ]]; then
  ok "handle_merge_gate: CI now failing on re-check -> gh pr merge NEVER called, approval treated as stale"
else
  bad "handle_merge_gate: a stale approval with red CI was merged anyway (or did not report it): $out"
fi
default_gh

# --- 2g2. drain_resolved_gates: EVERY resolved gate is handled (SB-952)

# Three gates resolved in one tick; exactly one was resumed. `claude` reads
# stdin, and resume_session ran inside `while read` over a process
# substitution, so the first resume swallowed the remaining lines and the
# other two humans' approvals were silently dropped — the tickets then got
# re-planned from scratch on a later tick.
gate_stub() {  # writes a gate.py stub whose `status` answers for any gate id
  cat >"$bin/gate-stub.py" <<'STUB'
import json, sys
gid = sys.argv[-1]
print(json.dumps({"kind": "plan", "session_id": "s-" + gid, "run_id": "r-" + gid, "note": ""}))
STUB
}
gate_stub

# A `claude` that drains stdin, exactly as the real one does. If the loop is
# not insulated from it, iterations 2 and 3 never happen.
cat >"$bin/claude" <<'STUB'
#!/usr/bin/env bash
cat >/dev/null            # consume whatever stdin is offered
echo "{\"status\":\"awaiting\"}"
STUB
chmod +x "$bin/claude"

resolved_tsv=$'g1\tSB-1\tapproved\ng2\tSB-2\tapproved\ng3\tSB-3\tapproved'
out="$(printf '%s\n' "$resolved_tsv" | run_sourced '
  GATE_PY="'"$bin"'/gate-stub.py"
  JSON_SCHEMA="{}"; ALLOWED_TOOLS=""; DISALLOWED_TOOLS=""
  SUMMARY_LINES=(); acted=0
  drain_resolved_gates
  printf "%s\n" "${SUMMARY_LINES[@]}"
')"

for t in SB-1 SB-2 SB-3; do
  if grep -q "Resumed $t" <<<"$out"; then
    ok "drain_resolved_gates: $t was handled"
  else
    bad "drain_resolved_gates: $t was NOT handled — the loop stopped early: $out"
  fi
done
rm -f "$bin/claude" "$bin/gate-stub.py"
default_gh

# --- 2g3. summary messages read like a teammate (SB-945)

# The Telegram summary is read on a phone by someone deciding whether they are
# on the hook. These pin the parts that made the old messages useless: no
# UUIDs, always a title and a link, and an idle tick that says nothing.
msg_out="$(run_sourced '
  LINEAR_SCRIPTS=""            # no network: ticket_title falls back to the key
  SUMMARY_LINES=()
  say "Started SB-1 — $(ticket_title SB-1)" "$(started_next_line awaiting)" "$(ticket_link SB-1)"
  printf "%s\n" "${SUMMARY_LINES[@]}"
')"

if grep -q 'https://linear.app/silverbeer/issue/SB-1' <<<"$msg_out"; then
  ok "summary: a ticket line carries a link"
else
  bad "summary: no Linear link in the message: $msg_out"
fi
if grep -qE '[0-9a-f]{8}-[0-9a-f]{4}-' <<<"$msg_out"; then
  bad "summary: a UUID leaked into the message: $msg_out"
else
  ok "summary: no session/run UUID in the message"
fi
if grep -q "I'll ask before making any changes" <<<"$msg_out"; then
  ok "summary: says what happens next, so the reader knows if they must act"
else
  bad "summary: no 'what happens next' line: $msg_out"
fi
if grep -q 'status ?' <<<"$msg_out"; then
  bad "summary: still emitting the literal 'status ?'"
else
  ok "summary: no 'status ?' placeholder"
fi

# An idle tick must post nothing at all — 48 "nothing to do" messages a day
# teach the reader to ignore the channel.
idle_out="$(run_sourced '
  GATEKEEPER_TG_TOKEN="t"; GATEKEEPER_TG_CHAT_ID="1"
  post_telegram_summary "/nonexistent" ""
')"
if grep -q 'nothing happened this tick' <<<"$idle_out"; then
  ok "summary: an idle tick sends no Telegram message"
else
  bad "summary: an idle tick still tried to post: $idle_out"
fi

# The plist runs this with /bin/bash, which is 3.2 on macOS. `declare -A` and
# other bash-4 constructs parse fine under the CI runner'"'"'s bash 5 and then
# fail on every tick on the only machine this is deployed to.
if /bin/bash -n "$RUN_SH" 2>"$WORK/bash32.err"; then
  ok "run.sh parses under /bin/bash ($(/bin/bash -c 'echo $BASH_VERSION'))"
else
  bad "run.sh does not parse under /bin/bash: $(cat "$WORK/bash32.err")"
fi

# --- 2g4. env.sh does NOT depend on the service-account token (SB-974)

# SB-953 asserted the opposite here: that env.sh must export
# OP_SERVICE_ACCOUNT_TOKEN so `op` could run headlessly on a tick. That premise
# was wrong — once a desktop account exists in ~/.config/op/config, `op read`
# prefers it and prompts anyway, with a valid token set. Secrets now come from
# files and env.sh needs no 1Password state at all, so the presence of a token
# file must make no difference to what it exports.
fake_home="$WORK/fakehome"
mkdir -p "$fake_home/.config/op"
printf 'not-a-real-token\n' >"$fake_home/.config/op/agent-token"
chmod 600 "$fake_home/.config/op/agent-token"

CR_ENV="$(cd "$(dirname "$RUN_SH")" && pwd)/env.sh"
env_out="$(env -i HOME="$fake_home" PATH="/usr/bin:/bin" CR_ENV="$CR_ENV" /bin/bash -c '
  . "$CR_ENV" 2>/dev/null
  [ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] && echo EXPORTED || echo CLEAN
' 2>/dev/null)"

if [ "$env_out" = "CLEAN" ]; then
  ok "env.sh: does not export a 1Password token — nothing on the tick path needs one"
else
  bad "env.sh exported OP_SERVICE_ACCOUNT_TOKEN — that is the SB-974 regression: op must not be reachable from a tick"
fi

# An absent secrets dir must stay a silent no-op, not an error: a laptop that
# was never provisioned still has to be able to source this.
env_out2="$(env -i HOME="$WORK/emptyhome" PATH="/usr/bin:/bin" CR_ENV="$CR_ENV" /bin/bash -c '
  . "$CR_ENV" 2>/dev/null; echo "rc=$?"
' 2>/dev/null)"
if [ "$env_out2" = "rc=0" ]; then
  ok "env.sh: no token file -> sources cleanly, stays a no-op"
else
  bad "env.sh: a missing token file broke sourcing ($env_out2)"
fi

# The secret must never be written into the chezmoi-managed plist.
PLIST_TMPL="$REPO/Library/LaunchAgents/io.silverbeer.cycle-runner.plist.tmpl"
if [ -f "$PLIST_TMPL" ] && grep -q "OP_SERVICE_ACCOUNT_TOKEN" "$PLIST_TMPL"; then
  bad "plist template references OP_SERVICE_ACCOUNT_TOKEN — a bearer token must not live in a managed file"
else
  ok "plist template carries no service-account token"
fi

# --- 2g5. no `op` on the tick path (SB-974)

# Two fixes (SB-953, SB-972) assumed exporting OP_SERVICE_ACCOUNT_TOKEN makes
# `op` headless. It does not: once a desktop account exists in
# ~/.config/op/config, `op read` prefers it and prompts, with a valid token set.
# A logging shim caught exactly that, and one call stayed wedged twelve minutes.
# A wedged `op` blocks every later call, so this can stop an unattended runner
# outright. Secrets come from files now; these assert it stays that way.
note "2g5. secrets come from files, not op"

for envsh in "$(dirname "$RUN_SH")/env.sh" "$REPO/dot_claude/skills/gatekeeper/scripts/env.sh"; do
  label="$(basename "$(dirname "$(dirname "$envsh")")")/env.sh"
  if grep -qE '^[^#]*\bop +(read|item|whoami|account)\b' "$envsh"; then
    bad "$label invokes op — that can wedge on a desktop prompt and stop the runner (SB-974)"
  else
    ok "$label: no op invocation"
  fi
done

# It must actually read the files, not merely avoid op.
sec_home="$WORK/sechome"; mkdir -p "$sec_home"
printf 'tok-claude' >"$sec_home/claude-token"
printf 'tok-tg'     >"$sec_home/telegram-token"
printf '4242'       >"$sec_home/telegram-chat-id"

got="$(env -i HOME="$WORK/nohome" PATH="/usr/bin:/bin" \
        CYCLE_RUNNER_SECRETS_DIR="$sec_home" \
        CR="$(dirname "$RUN_SH")" GK="$REPO/dot_claude/skills/gatekeeper/scripts" \
        /bin/bash -c '. "$CR/env.sh" 2>/dev/null; . "$GK/env.sh" 2>/dev/null
                      echo "${CLAUDE_CODE_OAUTH_TOKEN:-} ${GATEKEEPER_TG_TOKEN:-} ${GATEKEEPER_TG_CHAT_ID:-}"' 2>/dev/null)"
if [ "$got" = "tok-claude tok-tg 4242" ]; then
  ok "env.sh exports all three secrets from files, in a launchd-shaped env"
else
  bad "env.sh did not export the secrets from files (got '$got')"
fi

# A missing file must be a silent no-op, not an error that kills the tick.
rc=0
env -i HOME="$WORK/nohome" PATH="/usr/bin:/bin" CYCLE_RUNNER_SECRETS_DIR="$WORK/empty-secrets" \
  CR="$(dirname "$RUN_SH")" /bin/bash -c '. "$CR/env.sh"' >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then
  ok "env.sh: missing secret file -> sources cleanly, stays a no-op"
else
  bad "env.sh: a missing secret file broke sourcing (rc=$rc)"
fi

# --- 2h. scan_log_clean: the run-log scanner gates the outbound post (SB-943)

# The whole point of the scan is that a run log carrying a credential never
# reaches Telegram. These three cases pin the only behaviour that matters:
# clean -> 0, finding -> 1, scanner absent -> 2. Anything but 0 must leave the
# summary unposted, so a missing binary can never silently open the gate.
scan_log="$WORK/scan-run.log"
echo "cycle-runner: nothing interesting here" >"$scan_log"

gitleaks_stub() {  # EXIT_CODE
  cat >"$bin/gitleaks" <<STUB
#!/usr/bin/env bash
exit $1
STUB
  chmod +x "$bin/gitleaks"
}

gitleaks_stub 0
rc=0; run_sourced "scan_log_clean '$scan_log' '$WORK/scan.out'" >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 0 ]; then
  ok "scan_log_clean: clean log -> rc 0 (summary may be posted)"
else
  bad "scan_log_clean: a clean log did not return 0 (rc=$rc)"
fi

gitleaks_stub 1
rc=0; run_sourced "scan_log_clean '$scan_log' '$WORK/scan.out'" >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 1 ]; then
  ok "scan_log_clean: gitleaks finding -> rc 1 (post suppressed)"
else
  bad "scan_log_clean: a gitleaks finding did not return 1 (rc=$rc)"
fi

rm -f "$bin/gitleaks"
rc=0
# PATH is narrowed to the stub dir plus the system ones so the real gitleaks —
# brew installs it under /opt/homebrew/bin — cannot satisfy the lookup and mask
# the regression, while bash/mktemp/cp stay reachable.
out="$(PATH="$bin:/usr/bin:/bin" run_sourced "scan_log_clean '$scan_log' '$WORK/scan.out'" 2>&1)" || rc=$?
if [ "$rc" -eq 2 ] && [[ "$out" == *"cannot scan the run log"* ]]; then
  ok "scan_log_clean: scanner absent -> rc 2, fails closed (post suppressed)"
else
  bad "scan_log_clean: a missing gitleaks did not fail closed (rc=$rc): $out"
fi

# ---------------------------------------------------------------- verdict

[ "$fails" -eq 0 ] || die "check-cycle-runner-policy: $fails failure(s)"
note "check-cycle-runner-policy: all offline tests passed"
