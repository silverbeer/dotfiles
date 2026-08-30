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

# NEGATIVE: one test file survives but is thinned out below the floor —
# discovery succeeds, so this is the count-floor guard's own test, distinct
# from the "found nothing at all" case above.
test_test_count_below_the_floor_fails() {
  need_bin python3
  src="$(copy_source)"
  rm -f "$src"/dot_claude/skills/gatekeeper/tests/test_gate.py
  export REPO="$src"
  assert_fail check-gatekeeper.sh
  assert_out 'expected at least 25 tests to run'
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
