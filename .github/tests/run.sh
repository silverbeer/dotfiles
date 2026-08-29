#!/usr/bin/env bash
# Run the whole CI-check test suite.
#
#     bash .github/tests/run.sh            # everything
#     bash .github/tests/run.sh skill      # only files matching *skill*
#
# No network, no package install, no writes outside a mktemp -d. Checks whose
# binary is missing SKIP with a reason instead of failing, so this is runnable
# on a clean checkout; the summary reports skips separately from passes so a
# machine that is silently testing nothing is obvious.
set -uo pipefail

TESTS_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$TESTS_DIR/../.." && pwd)"

QE_RESULTS="$(mktemp "${TMPDIR:-/tmp}/qe-results.XXXXXX")"
export QE_RESULTS
trap 'rm -f "$QE_RESULTS"' EXIT INT TERM

filter="${1:-}"

printf 'dotfiles CI-check suite\n'
printf '  repo    %s\n' "$REPO_ROOT"
printf '  bash    %s\n' "${BASH_VERSION}"
printf '  tools  '
for b in chezmoi jq git shellcheck ruff gitleaks python3; do
  if command -v "$b" >/dev/null 2>&1; then printf ' +%s' "$b"; else printf ' -%s' "$b"; fi
done
printf '\n'

ran=0
for f in "$TESTS_DIR"/test_*.sh; do
  [ -f "$f" ] || continue
  case "$(basename -- "$f")" in
    *"$filter"*) ;;
    *) continue ;;
  esac
  name="$(basename -- "$f")"
  before="$(wc -l <"$QE_RESULTS" | tr -d ' ')"
  bash "$f"
  frc=$?
  after="$(wc -l <"$QE_RESULTS" | tr -d ' ')"

  if [ "$before" = "$after" ]; then
    # A file that registers nothing has died before run_tests, or forgot to
    # call it. Either way it is not testing anything and must not pass quietly.
    printf '  FAIL  %s produced no results (crashed, or never called run_tests)\n' "$name"
    printf 'FAIL\t%s\t<file produced no results>\n' "$name" >>"$QE_RESULTS"
  fi

  # The file's own exit status was thrown away. A file that registered one PASS
  # and then died — `exit 42` after run_tests, a `set -e` abort in teardown —
  # printed "1 passed, 0 failed" and run.sh exited 0. The wc -l tripwire above
  # only ever fires at zero.
  if [ "$frc" -ne 0 ]; then
    printf '  FAIL  %s exited %s after its tests ran\n' "$name" "$frc"
    printf 'FAIL\t%s\t<file exited %s>\n' "$name" "$frc" >>"$QE_RESULTS"
  fi

  # run_tests is the LAST line of every test file, so a test appended after it —
  # the natural editing motion — is defined but never enumerated and never runs.
  # Count the definitions in the file and insist that every one registered a
  # result. Definitions must be at column 0 to be counted; fixture bodies that
  # test_meta.sh writes out at runtime are deliberately not.
  defined="$(grep -cE '^test_[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\(\)' "$f" | tr -d ' ')"
  registered=$((after - before))
  if [ "$defined" -ne "$registered" ]; then
    printf '  FAIL  %s defines %s test(s) but registered %s\n' "$name" "$defined" "$registered"
    printf 'FAIL\t%s\t<defines %s test(s), registered %s — one defined after run_tests?>\n' \
      "$name" "$defined" "$registered" >>"$QE_RESULTS"
  fi
  ran=$((ran + 1))
done

if [ "$ran" -eq 0 ]; then
  printf '\nno test files matched %s\n' "'$filter'"
  exit 1
fi

pass="$(grep -c '^PASS' "$QE_RESULTS" || true)"
skipn="$(grep -c '^SKIP' "$QE_RESULTS" || true)"
failn="$(grep -c '^FAIL' "$QE_RESULTS" || true)"

printf '\n---------------------------------------------------------------\n'
if [ "$skipn" != "0" ]; then
  printf 'skipped:\n'
  grep '^SKIP' "$QE_RESULTS" | awk -F'\t' '{printf "  %-28s %-46s %s\n", $2, $3, $4}'
fi
if [ "$failn" != "0" ]; then
  printf 'failed:\n'
  grep '^FAIL' "$QE_RESULTS" | awk -F'\t' '{printf "  %-28s %s\n", $2, $3}'
fi
printf '%s passed, %s skipped, %s failed  (%s files)\n' "$pass" "$skipn" "$failn" "$ran"

[ "$failn" = "0" ] || exit 1

# In CI every tool is installed, so a skip is not honest reporting — it means an
# install silently stopped working and the suite went green while testing
# nothing. QE_NO_SKIPS=1 makes that a failure.
if [ "${QE_NO_SKIPS:-0}" = "1" ] && [ "$skipn" != "0" ]; then
  printf 'QE_NO_SKIPS is set: %s skipped test(s) is a failure here\n' "$skipn"
  exit 1
fi
exit 0
