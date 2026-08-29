#!/usr/bin/env bash
# check-ruff.sh and check-pins.sh.
#
# .ruff.toml pins BOTH the rule selection and the ruff version. Both are
# load-bearing and neither is obvious:
#
#   select = ["E", "F"]        ruff's default set is a curated list that
#                              excludes E501 and drifts between releases
#   required-version = "==..." without it, "ruff clean" means something
#                              different next month, on the same code
#
# The original ad-hoc test for this installed ruff 0.16.4 alongside 0.16.5.
# That cannot be a checked-in test: it needs the network and a second binary.
# Pointing the SAME ruff at a config demanding a different version proves the
# identical property offline, and check-pins.sh additionally catches the drift
# that would cause it — which the two-binary test never could.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

# ------------------------------------------------------- the version pin

test_ruff_version_is_pinned_identically_in_ci_and_config() {
  assert_ok check-pins.sh
  assert_out 'ruff pinned consistently at'
}

# NEGATIVE: bump one and not the other. On a real PR this surfaces as ruff
# aborting on every file, which reads like a broken lint rather than a pin.
test_pin_drift_between_ci_and_ruff_toml_is_rejected() {
  src="$(copy_source)"
  sed 's/^required-version = "==.*"$/required-version = "==0.16.4"/' \
    "$src/.ruff.toml" >"$src/.ruff.toml.new"
  mv "$src/.ruff.toml.new" "$src/.ruff.toml"
  grep -q '0.16.4' "$src/.ruff.toml" || fail "fixture did not change the pin"

  export REPO="$src"
  assert_fail check-pins.sh
  assert_out 'ruff pin drift'
}

# NEGATIVE: loosening required-version to a range is the tempting "fix" for the
# drift above, and it silently removes the guarantee entirely.
test_a_removed_required_version_is_rejected() {
  src="$(copy_source)"
  grep -v '^required-version' "$src/.ruff.toml" >"$src/.ruff.toml.new"
  mv "$src/.ruff.toml.new" "$src/.ruff.toml"

  export REPO="$src"
  assert_fail check-pins.sh
  assert_out "could not read an '==x.y.z' required-version"
}

# NEGATIVE: the pinned version is actually ENFORCED by ruff, not just written
# down. Same binary, a config demanding a version it is not.
test_ruff_refuses_to_run_against_a_mismatched_required_version() {
  need_bin ruff
  work="$(new_dir)"
  sed 's/^required-version = "==.*"$/required-version = "==0.0.1"/' \
    "$REPO_ROOT/.ruff.toml" >"$work/mismatch.toml"

  export RUFF_CONFIG="$work/mismatch.toml"
  # Exit 2, not 1: ruff refuses to start rather than reporting findings. Spelled
  # out because plain assert_fail now rejects 2 — that code otherwise means the
  # check script failed to parse or to execute.
  assert_fail_rc 2 check-ruff.sh
  assert_out 'does not match the running version'
}

# ------------------------------------------------------- the rule selection

test_tracked_python_is_clean() {
  need_bin ruff git
  assert_ok check-ruff.sh
  assert_out 'tracked *.py'
}

# NEGATIVE: F rules. If `select` were ever dropped, F401 would still be caught
# by ruff's defaults — so this one alone does not prove the selection matters.
test_an_unused_import_is_rejected() {
  need_bin ruff
  work="$(new_dir)"
  printf 'import os\n' >"$work/bad.py"

  assert_fail check-ruff.sh "$work/bad.py"
  assert_out 'F401'
}

test_an_undefined_name_is_rejected() {
  need_bin ruff
  work="$(new_dir)"
  printf 'def f():\n    return undefined_thing\n' >"$work/bad.py"

  assert_fail check-ruff.sh "$work/bad.py"
  assert_out 'F821'
}

# NEGATIVE: E501. This is the one that proves `select` is doing work — E501 is
# NOT in ruff's default rule set, so a config that lost `select = ["E", "F"]`
# would let this through.
test_an_over_long_line_is_rejected() {
  need_bin ruff
  work="$(new_dir)"
  python3 -c "print('x = \"' + 'a' * 130 + '\"')" >"$work/bad.py" 2>/dev/null \
    || printf 'x = "%s"\n' "$(printf 'a%.0s' $(seq 130))" >"$work/bad.py"

  assert_fail check-ruff.sh "$work/bad.py"
  assert_out 'E501'
}

# ...and the complement: exactly 120 characters is fine, 121 is not. The
# boundary is the whole point of pinning line-length.
test_line_length_boundary_is_120() {
  need_bin ruff python3
  work="$(new_dir)"
  python3 -c "print('x = \"' + 'a' * 114 + '\"')" >"$work/ok.py"
  [ "$(awk '{print length}' "$work/ok.py")" = "120" ] \
    || fail "fixture is $(awk '{print length}' "$work/ok.py") chars, wanted 120"
  assert_ok check-ruff.sh "$work/ok.py"

  python3 -c "print('x = \"' + 'a' * 115 + '\"')" >"$work/over.py"
  [ "$(awk '{print length}' "$work/over.py")" = "121" ] \
    || fail "fixture is not 121 chars"
  assert_fail check-ruff.sh "$work/over.py"
  assert_out 'E501'
}

run_tests
