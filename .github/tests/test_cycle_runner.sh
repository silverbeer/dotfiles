#!/usr/bin/env bash
# check-cycle-runner-policy.sh — offline tests for the cycle-runner skill
# (SB-929). The positive case proves the check passes on the current tree;
# each negative breaks ONE thing in a copy of the tree and asserts the check
# fails naming it.
# shellcheck disable=SC2016  # the edit()/grep fixtures quote run.sh's $-names literally, on purpose
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

PICK_PY=dot_claude/skills/cycle-runner/scripts/pick.py
RUN_SH=dot_claude/skills/cycle-runner/scripts/run.sh
TESTS_DIR=dot_claude/skills/cycle-runner/tests

# sed -i differs between BSD and GNU; write to a sibling and move.
edit() { sed "$1" "$2" >"$2.new" && mv "$2.new" "$2"; }

test_the_check_passes_on_the_current_tree() {
  need_bin python3 bash
  src="$(copy_source)"
  export REPO="$src"
  assert_ok check-cycle-runner-policy.sh
  assert_out 'ok   python: Ran '
  assert_out 'ok   no hand-rolled lock in run.sh'
  assert_out 'ok   summary: say() marks the tick as news'
  assert_out 'ok   summary: remind() is reported but never counts as news'
  assert_out 'ok   summary: silent=1 is honoured'
  assert_out 'ok   ci_state: gh reports success -> success'
  assert_out 'ok   ci_state: gh reports pending -> pending'
  assert_out 'ok   ci_state: gh reports failure -> failure'
  assert_out 'ok   handle_merge_gate: no pr_url file -> not merged'
  assert_out 'ok   handle_merge_gate: CI green -> gh pr merge --squash --delete-branch called'
  assert_out 'ok   handle_merge_gate: CI now failing on re-check -> gh pr merge NEVER called'
  assert_out 'check-cycle-runner-policy: all offline tests passed'
}

# NEGATIVE: the driven:agent-auto estimate gate is loosened from <=2 to <=20 —
# this is the exact number the whole "agent-auto only for tiny tickets" policy
# hangs on, so a slip here must fail loudly and name the test that caught it.
test_loosened_agent_auto_estimate_gate_fails_naming_the_test() {
  need_bin python3 bash
  src="$(copy_source)"
  edit 's/estimate <= 2 and/estimate <= 20 and/' "$src/$PICK_PY"
  grep -q 'estimate <= 20 and' "$src/$PICK_PY" || fail "fixture did not loosen the estimate gate"
  export REPO="$src"
  assert_fail check-cycle-runner-policy.sh
  assert_out 'pick.py unit tests failed'
  assert_out 'FAIL: test_agent_auto_estimate_3_is_a_policy_violation'
}

# NEGATIVE: a blocker that reached Canceled stops counting as closed — a live
# ticket would then sit in the ready queue forever behind a dead relation.
test_broken_closed_states_fails_the_canceled_blocker_test() {
  need_bin python3 bash
  src="$(copy_source)"
  edit 's/CLOSED_STATES = ("Done", "Canceled")/CLOSED_STATES = ("Done",)/' "$src/$PICK_PY"
  grep -q 'CLOSED_STATES = ("Done",)' "$src/$PICK_PY" || fail "fixture did not break CLOSED_STATES"
  export REPO="$src"
  assert_fail check-cycle-runner-policy.sh
  assert_out 'pick.py unit tests failed'
  assert_out 'FAIL: test_ready_once_the_blocker_is_canceled'
}

# NEGATIVE: the cycle-runner tests directory is missing entirely — must fail
# loudly, not silently report zero tests as green.
test_missing_tests_directory_fails() {
  need_bin python3 bash
  src="$(copy_source)"
  rm -rf "${src:?}/$TESTS_DIR"
  export REPO="$src"
  assert_fail check-cycle-runner-policy.sh
  assert_out "missing $src/$TESTS_DIR"
}

# NEGATIVE: the test file itself vanishes while the directory survives.
# `unittest discover` finds nothing to run; that is not a green run.
test_missing_test_files_fails() {
  need_bin python3 bash
  src="$(copy_source)"
  rm -f "$src/$TESTS_DIR"/test_*.py
  export REPO="$src"
  assert_fail check-cycle-runner-policy.sh
  assert_out 'pick.py unit tests failed'
  assert_out 'NO TESTS RAN'
}

# NEGATIVE: the hand-rolled lock comes back (SB-976). It was removed because
# `concurrencyPolicy: Forbid` on the CronJob is the same guarantee enforced by
# the thing that actually decides when a tick starts. Keeping both would be two
# mechanisms that can disagree about whether a run is in progress — the shape
# behind SB-949 and SB-952 — and it is easy to reintroduce "just for local
# runs", which is exactly when nobody would notice.
test_a_reintroduced_hand_rolled_lock_is_rejected() {
  need_bin python3 bash
  src="$(copy_source)"
  printf '\nacquire_lock() { mkdir "$LOCK_DIR" 2>/dev/null; }\n' >>"$src/$RUN_SH"
  grep -q 'acquire_lock' "$src/$RUN_SH" || fail "fixture did not reintroduce the lock"
  export REPO="$src"
  assert_fail check-cycle-runner-policy.sh
  assert_out 'the hand-rolled lock is back'
}

# NEGATIVE: handle_merge_gate's CI re-check is defeated (the "state != success"
# guard can never trigger), so an approval that went stale while a human sat
# on the Telegram gate would get merged anyway with red CI. This is exactly
# what "never trust the gate answer as a substitute for the CI check" means.
test_broken_ci_recheck_guard_fails_the_stale_approval_scenario() {
  need_bin python3 bash
  src="$(copy_source)"
  edit 's/if \[\[ "\$state" != "success" \]\]; then/if [[ "$state" == "__never__" ]]; then/' "$src/$RUN_SH"
  grep -q '"\$state" == "__never__"' "$src/$RUN_SH" || fail "fixture did not break the CI re-check guard"
  export REPO="$src"
  assert_fail check-cycle-runner-policy.sh
  assert_out 'a stale approval with red CI was merged anyway'
}

# NEGATIVE: remind() starts counting as news (SB-985). The split between say()
# and remind() is what decides whether a tick makes a sound, and getting it
# backwards fails in the quietest possible way — either real events go silent,
# or the reader is pinged for the third telling of something they already know.
# Neither raises an error; both just erode trust in the channel, which is what
# SB-945 was about.
test_remind_that_counts_as_news_is_rejected() {
  need_bin python3 bash
  src="$(copy_source)"
  edit 's/^remind() {  # HEADLINE NEXT LINK.*/remind() {\n  SUMMARY_HAS_NEWS=1/' "$src/$RUN_SH"
  grep -q 'remind() {' "$src/$RUN_SH" || fail "fixture did not rewrite remind()"
  export REPO="$src"
  assert_fail check-cycle-runner-policy.sh
  assert_out 'remind() did not behave as expected'
}

run_tests
