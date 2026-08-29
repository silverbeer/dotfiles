#!/usr/bin/env bash
# shellcheck shell=bash
#
# A small test harness. Deliberately not a framework: this repo has no package
# manager, no virtualenv and no test runner, and adding one in order to test six
# shell checks would be worse than the disease.
#
# Written for bash 3.2 (the /bin/bash that ships with macOS): no mapfile, no
# associative arrays, no `local -n`.
#
# A test file looks like:
#
#   . "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"
#
#   test_something() {
#     need_bin chezmoi                       # -> SKIP, with a reason, if absent
#     assert_ok   check-skill-exceptions.sh
#   }
#
#   run_tests
#
# Each test_* function runs in its own subshell with its own scratch dir, so a
# test that mutates a copy of the repo cannot leak into the next one. Nothing
# ever writes to the real repo or the real $HOME.

set -uo pipefail

TESTS_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH='' cd -- "$TESTS_DIR/../.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/.github/scripts"

# run.sh sets QE_RESULTS so tallies survive the per-file subprocess. Running a
# single test file directly gets its own.
if [ -z "${QE_RESULTS:-}" ]; then
  QE_RESULTS="$(mktemp "${TMPDIR:-/tmp}/qe-results.XXXXXX")"
  export QE_RESULTS
  QE_OWN_RESULTS=1
fi

QE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/qe-tests.XXXXXX")"
# Cleanup on interrupt and on failure, not just on a clean exit.
trap 'rm -rf "$QE_TMP"' EXIT INT TERM

QE_OUT=""
QE_RC=0

# ---------------------------------------------------------------- assertions

fail() { printf 'ASSERT: %s\n' "$*" >&2; exit 1; }

# Skip with a reason. Exit code 77 is the automake convention.
skip() { printf '%s\n' "$*" >"$QE_TMP/skip-reason"; exit 77; }

need_bin() {
  for _b in "$@"; do
    command -v "$_b" >/dev/null 2>&1 || skip "$_b is not installed"
  done
}

_dump() { sed 's/^/    | /' "$QE_OUT" >&2; }

# Run a check script. Name it bare (check-foo.sh) and it resolves against
# .github/scripts/. Output is captured to $QE_OUT for the assert_out family.
run_check() {
  _script="$1"; shift
  case "$_script" in
    /*) : ;;
    *) _script="$SCRIPTS_DIR/$_script" ;;
  esac
  [ -f "$_script" ] || fail "no such check script: $_script"
  QE_OUT="$(mktemp "$QE_TMP/out.XXXXXX")"
  # Checks under test are EXPECTED to fail; do not let set -e cut in here.
  set +e
  bash "$_script" "$@" >"$QE_OUT" 2>&1
  QE_RC=$?
  set -e
  return 0
}

assert_ok() {
  run_check "$@"
  [ "$QE_RC" -eq 0 ] && return 0
  printf 'ASSERT: expected %s to succeed, got exit %s. Output:\n' "$1" "$QE_RC" >&2
  _dump
  exit 1
}

# A check that FAILS is one that ran and said no. Exit 2, 126 and 127 are not
# that: they are bash refusing to parse the script, refusing to execute it, or
# not finding it. assert_fail accepted all three, so a check mangled past the
# point of parsing left its negative test green while nothing was being checked
# — measured with a check whose body was `set -euo pipefail` and a stray `fi`.
# run_check guards a MISSING script; it did not guard a broken one.
#
# A check that legitimately exits with one of those codes has to say so, with
# assert_fail_rc.
assert_fail() {
  run_check "$@"
  if [ "$QE_RC" -eq 0 ]; then
    printf 'ASSERT: expected %s to FAIL, but it exited 0. Output:\n' "$1" >&2
    _dump
    exit 1
  fi
  case "$QE_RC" in
    2|126|127)
      printf 'ASSERT: %s exited %s — that is a syntax or exec error, not the check failing. Use assert_fail_rc %s if it is intended. Output:\n' \
        "$1" "$QE_RC" "$QE_RC" >&2
      _dump
      exit 1
      ;;
  esac
  return 0
}

# For the deliberate exception: check-ruff.sh exits 2 when ruff itself refuses
# to run against a mismatched required-version, and that IS the behaviour under
# test.
assert_fail_rc() {
  _want="$1"; shift
  run_check "$@"
  [ "$QE_RC" -eq "$_want" ] && return 0
  printf 'ASSERT: expected %s to exit %s, got %s. Output:\n' "$1" "$_want" "$QE_RC" >&2
  _dump
  exit 1
}

# The point of a negative test is that the check failed FOR THE RIGHT REASON —
# a typo in the scratch fixture also produces a non-zero exit.
assert_out() {
  grep -qF -- "$1" "$QE_OUT" && return 0
  printf 'ASSERT: expected output to contain %s. Output:\n' "'$1'" >&2
  _dump
  exit 1
}

assert_not_out() {
  grep -qF -- "$1" "$QE_OUT" || return 0
  printf 'ASSERT: expected output NOT to contain %s. Output:\n' "'$1'" >&2
  _dump
  exit 1
}

# ------------------------------------------------------------------ fixtures

# A writable copy of the chezmoi source tree, for tests that must mutate it.
#
# TRACKED FILES ONLY, taken from the working tree. `cp -R "$REPO_ROOT/."` copied
# whatever else happened to be lying around and then `git add -A` staged it, so
# one stray untracked scratch script with a shellcheck error made
# check-shellcheck.sh fail here for a reason that had nothing to do with the
# fixture. Measured: a test identical to the broken-modify_settings negative
# except that it injected NO breakage at all still reported PASS. It is also the
# likeliest way t2/, h2/ and root.txt ended up in the repo root during SB-904.
#
# Not `git archive HEAD`: these checks must see the working tree — the edit
# under review — not the last commit.
#
# The real .git is left behind and a fresh one initialised instead. That means a
# mutating test can never reach the real repository's history; the fresh one is
# needed because several checks enumerate their inputs with `git ls-files`, and
# without an index they would exit non-zero for the wrong reason — which is how
# a negative test quietly stops testing anything.
copy_source() {
  _dst="$(mktemp -d "$QE_TMP/src.XXXXXX")"
  ( cd "$REPO_ROOT" && git ls-files -z | tar -cf - --null -T - ) \
    | ( cd "$_dst" && tar -xf - ) \
    || fail "copy_source could not copy the tracked tree (a tracked file missing from the working tree?)"
  [ -f "$_dst/.chezmoiignore" ] || fail "copy_source produced no .chezmoiignore"

  git -c init.defaultBranch=main init -q "$_dst" >/dev/null 2>&1 \
    || fail "copy_source could not git init"
  git -C "$_dst" add -A >/dev/null 2>&1 || fail "copy_source could not git add"
  [ -n "$(git -C "$_dst" ls-files '*.sh')" ] \
    || fail "copy_source indexed no *.sh — later checks would fail for the wrong reason"
  printf '%s' "$_dst"
}

new_dir() { mktemp -d "$QE_TMP/d.XXXXXX"; }

# ------------------------------------------------------------------- runner

run_tests() {
  _file="$(basename -- "$0")"
  printf '\n== %s\n' "$_file"
  # `declare -F` prints "declare -f <name>"; this is the bash 3.2-safe way to
  # enumerate tests without an associative array.
  # shellcheck disable=SC2046
  for _t in $(declare -F | sed 's/^declare -f //' | grep '^test_' | sort); do
    _desc="$(printf '%s' "$_t" | sed 's/^test_//; s/_/ /g')"
    rm -f "$QE_TMP/skip-reason"
    (
      set -e
      WORK="$(new_dir)"; export WORK
      unset DEST REPO GITLEAKS_CONFIG RUFF_CONFIG
      "$_t"
    )
    _rc=$?
    if [ "$_rc" -eq 0 ]; then
      printf '  PASS  %s\n' "$_desc"
      printf 'PASS\t%s\t%s\n' "$_file" "$_desc" >>"$QE_RESULTS"
    elif [ "$_rc" -eq 77 ]; then
      _why="$(cat "$QE_TMP/skip-reason" 2>/dev/null || echo 'no reason given')"
      printf '  SKIP  %s  (%s)\n' "$_desc" "$_why"
      printf 'SKIP\t%s\t%s\t%s\n' "$_file" "$_desc" "$_why" >>"$QE_RESULTS"
    else
      printf '  FAIL  %s\n' "$_desc"
      printf 'FAIL\t%s\t%s\n' "$_file" "$_desc" >>"$QE_RESULTS"
    fi
  done

  if [ "${QE_OWN_RESULTS:-0}" = "1" ] && grep -q '^FAIL' "$QE_RESULTS"; then
    exit 1
  fi
  return 0
}
