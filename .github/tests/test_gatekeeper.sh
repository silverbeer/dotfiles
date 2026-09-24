#!/usr/bin/env bash
# check-gatekeeper.sh — offline unit tests for the gatekeeper skill's
# Telegram transport and dual-channel gate logic (SB-508). The positive case
# proves the check passes on the current tree; each negative breaks ONE thing
# in a copy of the tree and asserts the check fails naming it.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

GATE_PY=dot_claude/skills/gatekeeper/scripts/gate.py

# sed -i differs between BSD and GNU; write to a sibling and move.
edit() { sed "$1" "$2" >"$2.new" && mv "$2.new" "$2"; }

test_the_check_passes_on_the_current_tree() {
  need_bin python3
  src="$(copy_source)"
  export REPO="$src"
  assert_ok check-gatekeeper.sh
  assert_out 'OK'
  assert_out 'check-gatekeeper: all offline tests passed'
}

# NEGATIVE: break parse_decision so "reject: reason" no longer carries the
# reason through. This is the exact string the Linear and Telegram reject
# paths both depend on, so its test names should fail and say so.
test_broken_parse_decision_fails_naming_the_tests() {
  need_bin python3
  src="$(copy_source)"
  edit 's/(rest.strip() or None) if sep else None/None/' "$src/$GATE_PY"
  grep -q 'return verb.strip().lower(), None$' "$src/$GATE_PY" \
    || fail "fixture did not break parse_decision"
  export REPO="$src"
  assert_fail check-gatekeeper.sh
  assert_out 'gatekeeper unit tests failed'
  assert_out 'FAIL: test_linear_reject_with_reason_is_parsed'
  assert_out 'FAIL: test_telegram_free_text_reject_with_reason_decides'
}

# NEGATIVE: the gatekeeper tests directory is missing entirely (e.g. a
# rename that broke discovery). Must fail loudly, not report zero tests green.
test_missing_tests_directory_fails() {
  need_bin python3
  src="$(copy_source)"
  rm -rf "${src:?}/dot_claude/skills/gatekeeper/tests"
  export REPO="$src"
  assert_fail check-gatekeeper.sh
  assert_out "missing $src/dot_claude/skills/gatekeeper/tests"
}

# NEGATIVE: the python tests themselves vanish while the directory survives.
# `unittest discover` errors out with nothing to run; this must not be a
# green run.
test_missing_test_files_fails() {
  need_bin python3
  src="$(copy_source)"
  rm -f "$src"/dot_claude/skills/gatekeeper/tests/test_*.py
  export REPO="$src"
  assert_fail check-gatekeeper.sh
  assert_out 'gatekeeper unit tests failed'
  assert_out 'NO TESTS RAN'
}

# NEGATIVE: one test file vanishes while the others still pass — discovery
# succeeds, so this is the count-floor guard's own test, distinct from the
# "found nothing at all" case above. The SMALLEST file is the one removed: the
# floor exists so that losing any one file is caught, and the smallest is the
# hardest case. (Not test_gate.py any more: test_listen.py imports its
# fixture, so removing it fails the run for a different reason.)
test_test_count_below_the_floor_fails() {
  need_bin python3
  src="$(copy_source)"
  rm -f "$src"/dot_claude/skills/gatekeeper/tests/test_inbox.py
  export REPO="$src"
  assert_fail check-gatekeeper.sh
  assert_out 'expected at least 160 tests to run'
}

# NEGATIVE: the runner's poll starts reading Telegram again (SB-951). One
# getUpdates reader per bot token is the invariant the listener Deployment
# exists for; a second one in `gate.py poll` is a 409 for both. The tests that
# say so by name must fail.
test_poll_reading_telegram_again_fails_naming_the_tests() {
  need_bin python3
  src="$(copy_source)"
  edit 's/^        self.retry_pending()$/        self.transport.get_updates(offset=0, timeout=0, allowed_updates=[]); self.retry_pending()/' \
    "$src/$GATE_PY"
  grep -q 'self.transport.get_updates(offset=0' "$src/$GATE_PY" \
    || fail "fixture did not put a getUpdates call into poll_once"
  export REPO="$src"
  assert_fail check-gatekeeper.sh
  assert_out 'FAIL: test_poll_once_never_calls_get_updates'
  assert_out 'FAIL: test_listen_py_is_the_only_caller_of_get_updates'
}

# NEGATIVE: the tap is no longer saved before it is applied (SB-951). With
# Linear down, decide() fails and there is then nothing on disk to retry — the
# human's approval is gone, which is SB-950 again by a different route.
test_dropping_the_durable_save_before_decide_fails() {
  need_bin python3
  src="$(copy_source)"
  edit '/# the tap is durable from here (SB-951)/d' "$src/$GATE_PY"
  grep -q 'the tap is durable from here' "$src/$GATE_PY" \
    && fail "fixture did not remove the durable save"
  export REPO="$src"
  assert_fail check-gatekeeper.sh
  assert_out 'test_linear_down_keeps_the_decision_and_the_next_loop_applies_it'
}

# NEGATIVE: the marker helper in fakes.py stops matching FakeLinear's
# issue_comments query, so every Linear-channel test loses its comment feed.
test_broken_fake_linear_fails_the_linear_channel_tests() {
  need_bin python3
  src="$(copy_source)"
  fakes="$src/dot_claude/skills/gatekeeper/tests/fakes.py"
  edit 's/if "comments(first" in query:/if False:/' "$fakes"
  grep -q 'if False:' "$fakes" || fail "fixture did not break FakeLinear"
  export REPO="$src"
  assert_fail check-gatekeeper.sh
  assert_out 'gatekeeper unit tests failed'
  assert_out 'ERROR: test_linear_approve_with_note_is_parsed'
}

run_tests
