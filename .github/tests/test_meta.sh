#!/usr/bin/env bash
# Tests about the test suite and its wiring to ci.yml.
#
# The whole point of extracting the checks into .github/scripts/ was that there
# is exactly ONE copy of each. If a check drifts back into a `run:` block, the
# tests here go on passing against the extracted copy while CI runs the inline
# one — which is strictly worse than having no tests at all. These tests are
# what stops that.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

WORKFLOW="$REPO_ROOT/.github/workflows/ci.yml"

test_every_check_script_is_called_by_the_workflow() {
  missing=""
  for s in "$REPO_ROOT"/.github/scripts/check-*.sh; do
    n="$(basename "$s")"
    grep -q "scripts/$n" "$WORKFLOW" || missing="$missing $n"
  done
  [ -z "$missing" ] || fail "check scripts never called by ci.yml:$missing"
}

test_every_check_script_is_exercised_by_a_test() {
  missing=""
  for s in "$REPO_ROOT"/.github/scripts/check-*.sh; do
    n="$(basename "$s")"
    grep -qF "$n" "$TESTS_DIR"/test_*.sh || missing="$missing $n"
  done
  [ -z "$missing" ] || fail "check scripts with no test:$missing"
}

# The reason the extraction was worth doing. If any of these markers reappear
# inside ci.yml, a check has been re-inlined and now exists twice.
test_no_check_logic_has_drifted_back_into_the_workflow() {
  bad=""
  for marker in 'chezmoi --source' 'jq -n' 'execute-template' 'gitleaks git' 'gitleaks dir' 'ruff check'; do
    if grep -qF -- "$marker" "$WORKFLOW"; then
      bad="$bad
  $marker"
    fi
  done
  [ -z "$bad" ] || fail "check logic is inline in ci.yml again, so it now exists twice:$bad"
}

test_every_script_and_test_parses() {
  for f in "$REPO_ROOT"/.github/scripts/*.sh "$TESTS_DIR"/*.sh; do
    bash -n "$f" || fail "$f does not parse"
  done
}

test_every_check_script_is_executable() {
  bad=""
  for s in "$REPO_ROOT"/.github/scripts/check-*.sh "$TESTS_DIR"/run.sh; do
    [ -x "$s" ] || bad="$bad $(basename "$s")"
  done
  [ -z "$bad" ] || fail "not executable:$bad"
}

test_the_suite_and_scripts_are_shellcheck_clean() {
  need_bin shellcheck
  # These files become part of the tracked *.sh set, so the shell job will lint
  # them anyway. Catching it here means the failure names itself.
  shellcheck "$REPO_ROOT"/.github/scripts/*.sh "$TESTS_DIR"/*.sh \
    || fail "the CI scripts or the test suite are not shellcheck-clean"
}

# The all-green job is the ONE check name branch protection hangs off, and it
# runs `if: always()`, so its condition is the only thing standing between a job
# that never ran and a mergeable PR. GitHub has four result values; the gate
# named only 'failure' and 'cancelled', which left 'skipped' green.
#
# This is a static assertion because a workflow cannot be run locally. It is the
# only coverage this gate has.
test_the_all_green_gate_fails_closed() {
  missing=""
  for result in failure cancelled skipped; do
    grep -qF "contains(needs.*.result, '$result')" "$WORKFLOW" \
      || missing="$missing $result"
  done
  [ -z "$missing" ] \
    || fail "the all-green gate does not fail on:$missing — a job with that result would leave the PR mergeable"
}

# ci.yml's own env block claims every tool is pinned "by version AND by
# checksum". It was not: chezmoi came from an unchecksummed `curl | sh` on both
# matrix legs, in the job that renders every template and pipes them through
# bash. Nothing may execute a downloaded script without verifying it first.
test_no_unverified_installer_is_piped_into_a_shell() {
  # Comment lines are skipped: the comment recording WHY chezmoi is no longer
  # installed this way names the construct, and would otherwise trip its own
  # guard.
  hits="$(awk '
    /^[[:space:]]*#/ { next }
    /curl[^|]*\|[[:space:]]*(sh|bash)([[:space:]]|$)/ { print FNR ": " $0; next }
    /sh -c "\$\(curl/ { print FNR ": " $0 }
  ' "$WORKFLOW")"
  [ -z "$hits" ] || fail "ci.yml pipes a downloaded script straight into a shell:
$hits"
}

# ...and the complement: every release asset it downloads is checksummed. One
# sha256 verification per curl of a tarball.
test_every_downloaded_asset_is_checksummed() {
  # shellcheck disable=SC2016  # the $RUNNER_TEMP here is a literal to match on
  downloads="$(grep -cE '^[[:space:]]*curl .*-o "\$RUNNER_TEMP/' "$WORKFLOW")"
  verified="$(grep -cE 'sha256sum -c -|shasum -a 256 -c -|verify "\$' "$WORKFLOW")"
  [ "$downloads" -gt 0 ] || fail "no asset downloads found in ci.yml — has the install shape changed?"
  [ "$verified" -ge "$downloads" ] \
    || fail "ci.yml downloads $downloads asset(s) but has only $verified checksum verification(s)"
}

# The gitleaks fixture is assembled at runtime so that no credential-shaped
# literal exists in this repository. If one is ever pasted in, the repo's own
# gitleaks job starts failing and the tempting fix is a path allowlist for
# .github/tests — which would blind the scanner to a real key committed here.
test_no_credential_shaped_literal_in_the_test_suite() {
  hits="$(grep -rlE '(A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}' "$TESTS_DIR" || true)"
  [ -z "$hits" ] || fail "AWS-key-shaped literal in the test suite:
$hits
Assemble it from parts at runtime instead (see fake_key in test_gitleaks.sh)."
}

# Skips must be visible. A machine missing every tool should report six skips,
# not a green run — that is the difference between "tested" and "not tested".
test_a_missing_binary_skips_rather_than_fails() {
  work="$(new_dir)"
  # Indented inside the heredoc on purpose: run.sh counts `^test_` at column 0
  # in each test FILE to catch a test defined after run_tests, and a fixture
  # body sitting at column 0 here would be counted as one of THIS file's tests.
  cat >"$work/test_fixture.sh" <<'FIXTURE'
#!/usr/bin/env bash
. "$HARNESS"
  test_needs_a_tool_that_cannot_exist() {
    need_bin definitely-not-a-real-binary-xyz
    fail "should have skipped before reaching here"
  }
run_tests
FIXTURE
  results="$work/results.txt"
  : >"$results"
  HARNESS="$TESTS_DIR/harness.sh" QE_RESULTS="$results" \
    bash "$work/test_fixture.sh" >"$work/out.txt" 2>&1

  grep -q '^SKIP' "$results" \
    || fail "a missing binary did not register as a SKIP: $(cat "$results")"
  grep -q 'definitely-not-a-real-binary-xyz is not installed' "$results" \
    || fail "the skip carried no usable reason: $(cat "$results")"
  grep -q '^PASS' "$results" && fail "a skip was recorded as a pass"
  grep -q '^FAIL' "$results" && fail "a skip was recorded as a failure"
  return 0
}

# ...and the complement: a genuinely failing test must not be swallowed.
test_a_failing_test_is_reported_as_a_failure() {
  work="$(new_dir)"
  cat >"$work/test_fixture.sh" <<'FIXTURE'
#!/usr/bin/env bash
. "$HARNESS"
  test_that_always_fails() { fail "deliberate"; }
run_tests
FIXTURE
  results="$work/results.txt"
  : >"$results"
  HARNESS="$TESTS_DIR/harness.sh" QE_RESULTS="$results" \
    bash "$work/test_fixture.sh" >"$work/out.txt" 2>&1

  grep -q '^FAIL' "$results" || fail "a failing test was not recorded as FAIL"
  grep -q '^PASS' "$results" && fail "a failing test was recorded as a pass"
  return 0
}

# run.sh must not report success for a file that died before registering
# anything — the classic way a suite silently stops running.
test_a_crashing_test_file_is_reported_not_ignored() {
  work="$(new_dir)"
  fake_tests="$work/tests"; mkdir -p "$fake_tests"
  printf '#!/usr/bin/env bash\nexit 3\n' >"$fake_tests/test_crash.sh"
  cp "$TESTS_DIR/run.sh" "$fake_tests/run.sh"

  set +e
  bash "$fake_tests/run.sh" >"$work/out.txt" 2>&1
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "run.sh exited 0 despite a crashing test file: $(cat "$work/out.txt")"
  grep -q 'produced no results' "$work/out.txt" \
    || fail "run.sh did not explain the crash: $(cat "$work/out.txt")"
}

# assert_fail accepted ANY non-zero exit, including 2 — bash refusing to parse
# the check script. So a check mangled past the point of parsing left every one
# of its negative tests green while nothing was being checked at all. run_check
# guards a MISSING script; it did not guard a broken one.
test_assert_fail_rejects_a_check_that_does_not_parse() {
  work="$(new_dir)"
  printf '#!/usr/bin/env bash\nset -euo pipefail\nfi\n' >"$work/check-broken.sh"
  { printf '#!/usr/bin/env bash\n'
    printf '. "%s"\n' "$TESTS_DIR/harness.sh"
    printf 'test_a_check_that_cannot_parse() { assert_fail "%s"; }\n' "$work/check-broken.sh"
    printf 'run_tests\n'
  } >"$work/test_fixture.sh"

  results="$work/results.txt"
  : >"$results"
  QE_RESULTS="$results" bash "$work/test_fixture.sh" >"$work/out.txt" 2>&1

  grep -q '^FAIL' "$results" \
    || fail "assert_fail accepted a check that does not parse: $(cat "$work/out.txt")"
  grep -q 'syntax or exec error' "$work/out.txt" \
    || fail "the failure did not say why: $(cat "$work/out.txt")"
  return 0
}

# A test file's own exit status was discarded. A file that registered one PASS
# and then died reported "1 passed, 0 failed" and run.sh exited 0 — the wc -l
# tripwire above only ever fires at zero results.
#
# The fixture is built with printf rather than a heredoc so that its `test_`
# definitions land at column 0 in the GENERATED file (where run.sh counts them)
# without sitting at column 0 in this one (where they would be counted as this
# file's tests).
test_a_test_file_that_exits_nonzero_is_reported() {
  work="$(new_dir)"
  fake_tests="$work/tests"; mkdir -p "$fake_tests"
  cp "$TESTS_DIR/run.sh" "$fake_tests/run.sh"
  { printf '#!/usr/bin/env bash\n'
    printf '. "%s"\n' "$TESTS_DIR/harness.sh"
    printf 'test_that_passes() { return 0; }\n'
    printf 'run_tests\n'
    printf 'exit 42\n'
  } >"$fake_tests/test_dies.sh"

  set +e
  bash "$fake_tests/run.sh" >"$work/out.txt" 2>&1
  rc=$?
  set -e
  [ "$rc" -ne 0 ] \
    || fail "run.sh exited 0 for a file that exited 42: $(cat "$work/out.txt")"
  grep -q 'exited 42' "$work/out.txt" \
    || fail "run.sh did not name the exit status: $(cat "$work/out.txt")"
}

# run_tests is the LAST line of every test file, so a test appended after it is
# defined but never enumerated. A fixture whose appended body is `fail` reported
# "1 passed, 0 failed".
test_a_test_defined_after_run_tests_is_reported() {
  work="$(new_dir)"
  fake_tests="$work/tests"; mkdir -p "$fake_tests"
  cp "$TESTS_DIR/run.sh" "$fake_tests/run.sh"
  { printf '#!/usr/bin/env bash\n'
    printf '. "%s"\n' "$TESTS_DIR/harness.sh"
    printf 'test_that_passes() { return 0; }\n'
    printf 'run_tests\n'
    printf 'test_appended_after_run_tests() { fail "never runs"; }\n'
  } >"$fake_tests/test_appended.sh"

  set +e
  bash "$fake_tests/run.sh" >"$work/out.txt" 2>&1
  rc=$?
  set -e
  [ "$rc" -ne 0 ] \
    || fail "run.sh exited 0 for a file with an unenumerated test: $(cat "$work/out.txt")"
  grep -q 'defines 2 test(s) but registered 1' "$work/out.txt" \
    || fail "run.sh did not report the count mismatch: $(cat "$work/out.txt")"
}

run_tests
