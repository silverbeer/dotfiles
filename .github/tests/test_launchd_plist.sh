#!/usr/bin/env bash
# check-launchd-plist.sh — offline tests for the cycle-runner launchd agent
# template (SB-930). The positive case proves the check passes on the
# current tree; each negative breaks ONE thing in a copy of the tree and
# asserts the check fails naming it.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

TMPL=Library/LaunchAgents/io.silverbeer.cycle-runner.plist.tmpl

# sed -i differs between BSD and GNU; write to a sibling and move.
edit() { sed "$1" "$2" >"$2.new" && mv "$2.new" "$2"; }

test_the_check_passes_on_the_current_tree() {
  need_bin chezmoi jq
  src="$(copy_source)"
  export REPO="$src"
  assert_ok check-launchd-plist.sh
  assert_out 'hostname=Toms-Mac-mini -> '
  assert_out 'hostname=some-other-mac -> 0 bytes (correct)'
  assert_out 'no plist on disk (correct)'
  assert_out 'offers to create the plist (correct)'
  assert_out 'check-launchd-plist: all checks passed'
}

# NEGATIVE: the hostname gate is loosened so ANY machine gets the agent
# (the exact class of mistake StartInterval-on-every-laptop would be).
test_loosened_hostname_gate_fails() {
  need_bin chezmoi jq
  src="$(copy_source)"
  edit 's/eq \.chezmoi\.hostname "Toms-Mac-mini"/or true true/' "$src/$TMPL"
  grep -q 'or true true' "$src/$TMPL" || fail "fixture did not loosen the hostname gate"
  export REPO="$src"
  assert_fail check-launchd-plist.sh
  assert_out "byte(s) — every other Mac must get nothing"
}

# NEGATIVE: the gate is tightened to the point that even the mini itself no
# longer matches (a stray character, the classic hostname-typo failure).
test_hostname_typo_fails() {
  need_bin chezmoi jq
  src="$(copy_source)"
  edit 's/Toms-Mac-mini/Toms-Mac-Mini/' "$src/$TMPL"
  export REPO="$src"
  assert_fail check-launchd-plist.sh
  assert_out 'rendered to 0 bytes — the mini must get a real plist'
}

# NEGATIVE: StartInterval drifts off 1800 (30 min) — the number the whole
# "never more than one tick's worth of work queued" design leans on.
test_wrong_start_interval_fails() {
  need_bin chezmoi jq
  src="$(copy_source)"
  edit 's/<integer>1800<\/integer>/<integer>60<\/integer>/' "$src/$TMPL"
  export REPO="$src"
  assert_fail check-launchd-plist.sh
  assert_out "is missing '<integer>1800</integer>'"
}

# NEGATIVE: RunAtLoad flips to true — the plist would fire a cycle the
# instant the mini logs in, before a human has looked at the box.
test_run_at_load_true_fails() {
  need_bin chezmoi jq
  src="$(copy_source)"
  edit 's/<false\/>/<true\/>/' "$src/$TMPL"
  grep -q '<true/>' "$src/$TMPL" || fail "fixture did not flip RunAtLoad to true"
  export REPO="$src"
  assert_fail check-launchd-plist.sh
  assert_out "is missing '<false/>'"
}

run_tests
