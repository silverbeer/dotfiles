#!/usr/bin/env bash
# shellcheck disable=SC2016  # fixtures are literal shell/jq source; expansion
#                             is exactly what must NOT happen here.
# shellcheck disable=SC2031  # run_tests calls every test_* in its own subshell
#                             by design, so exporting REPO inside one is the
#                             intended scoping, not a lost write.
# check-apply-dry-run.sh and check-no-bare-apply.sh.
#
# The HARD RULE at the top of ci.yml: every apply is --dry-run, because a bare
# `chezmoi apply` executes the run_* scripts, and those call `op read` and
# `brew install`. Two separate guards, tested separately:
#
#   check-apply-dry-run.sh   the dry run really writes nothing
#   check-no-bare-apply.sh   the --dry-run flag is still on every call site
#
# The second exists because the first CANNOT catch a removed flag: without it
# the apply would do real work and still report success.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

test_dry_run_into_an_empty_home_writes_nothing() {
  need_bin chezmoi jq
  assert_ok check-apply-dry-run.sh
  assert_out 'pass 1: apply --dry-run into an empty HOME'
  assert_out 'pass 2: apply --dry-run over an existing settings.json'
}

# The emptiness assertion is the load-bearing half of pass 1. Exercise it
# directly against a dirty directory — a real dry run will not produce one, so
# this is the only way to prove the assertion can fail at all.
test_empty_dir_assertion_rejects_a_dirty_dir() {
  work="$(new_dir)"
  dirty="$work/dirty"; mkdir -p "$dirty/.claude"
  printf 'x\n' >"$dirty/.claude/settings.json"

  out="$work/out.txt"
  set +e
  ( set -euo pipefail
    # shellcheck source=../scripts/lib.sh
    WORK="$work" DEST="$work/home" . "$SCRIPTS_DIR/lib.sh"
    assert_dir_empty "$dirty" "should have been empty"
  ) >"$out" 2>&1
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "assert_dir_empty passed on a directory with 2 entries"
  grep -q 'should have been empty' "$out" || fail "no error message: $(cat "$out")"
  grep -q 'found 2 path' "$out" || fail "wrong path count: $(cat "$out")"
}

# ...and the complement, which is how that assertion could have gone quiet: a
# directory that does not exist is not an empty one. `find` on a missing path
# prints nothing and reports the problem on stderr, so the count was 0 and the
# assertion passed — against a $DEST that had never been created.
test_empty_dir_assertion_rejects_a_missing_dir() {
  work="$(new_dir)"
  out="$work/out.txt"
  set +e
  ( set -euo pipefail
    # shellcheck source=../scripts/lib.sh
    WORK="$work" DEST="$work/home" . "$SCRIPTS_DIR/lib.sh"
    assert_dir_empty "$work/never-created" "should have existed and been empty"
  ) >"$out" 2>&1
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "assert_dir_empty passed on a directory that does not exist"
  grep -q 'no such directory' "$out" || fail "no explanation: $(cat "$out")"
}

test_unchanged_assertion_rejects_a_modified_file() {
  work="$(new_dir)"
  printf 'before\n' >"$work/a"
  printf 'after\n' >"$work/b"

  out="$work/out.txt"
  set +e
  ( set -euo pipefail
    # shellcheck source=../scripts/lib.sh
    WORK="$work" DEST="$work/home" . "$SCRIPTS_DIR/lib.sh"
    assert_unchanged "$work/a" "$work/b" "file was modified"
  ) >"$out" 2>&1
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "assert_unchanged passed on two different files"
  grep -q 'file was modified' "$out" || fail "no error message: $(cat "$out")"
}

# cm() has to be hermetic, and HOME alone did not make it so: chezmoi reads
# $XDG_CONFIG_HOME/chezmoi/chezmoi.toml and only falls back to $HOME/.config
# when that variable is unset. On any machine exporting XDG_CONFIG_HOME — routine
# on Linux — the machine's own chezmoi.toml was feeding these checks. Measured on
# chezmoi v2.70.0: the injected [data] key below rendered.
test_a_foreign_xdg_config_cannot_influence_a_render() {
  need_bin chezmoi
  work="$(new_dir)"
  mkdir -p "$work/xdg/chezmoi" "$work/src" "$work/home"
  printf '[data]\n  injected = "FOREIGN-CONFIG-WAS-READ"\n' \
    >"$work/xdg/chezmoi/chezmoi.toml"

  out="$work/out.txt"
  set +e
  ( set -euo pipefail
    export XDG_CONFIG_HOME="$work/xdg" XDG_CACHE_HOME="$work/xdgcache" \
      XDG_DATA_HOME="$work/xdgdata"
    # Exported, not prefixed onto the `.`: a prefix assignment on a sourced file
    # lasts only for that file, and cm() reads REPO and DEST when it is CALLED.
    # shellcheck disable=SC2030  # scoping this to the subshell is the point
    export REPO="$work/src" WORK="$work" DEST="$work/home"
    # shellcheck source=../scripts/lib.sh
    . "$SCRIPTS_DIR/lib.sh"
    cm execute-template '{{ .injected }}'
  ) >"$out" 2>&1
  set -e

  grep -q 'FOREIGN-CONFIG-WAS-READ' "$out" \
    && fail "cm() read a chezmoi.toml from a foreign XDG_CONFIG_HOME: $(cat "$out")"
  return 0
}

# ------------------------------------------------- the --dry-run flag itself

test_every_apply_call_site_carries_dry_run() {
  assert_ok check-no-bare-apply.sh
  assert_out 'carry --dry-run'
}

# NEGATIVE: strip the flag from the check script. This is the edit that would
# make a runner run `op read` and `brew install` for real.
test_dropping_dry_run_from_a_check_script_is_rejected() {
  src="$(copy_source)"
  target="$src/.github/scripts/check-apply-dry-run.sh"
  sed 's/apply --dry-run -v/apply -v/' "$target" >"$target.new"
  mv "$target.new" "$target"
  grep -q 'cm apply -v' "$target" || fail "fixture did not strip --dry-run"

  export REPO="$src"
  assert_fail check-no-bare-apply.sh
  assert_out 'chezmoi apply without --dry-run'
}

# NEGATIVE: the same edit made inline in the workflow.
test_dropping_dry_run_in_the_workflow_is_rejected() {
  src="$(copy_source)"
  wf="$src/.github/workflows/ci.yml"
  printf '          chezmoi --source "$PWD" apply -v\n' >>"$wf"

  export REPO="$src"
  assert_fail check-no-bare-apply.sh
  assert_out 'ci.yml'
  assert_out 'without --dry-run'
}

# NEGATIVE: an inline comment mentioning the flag. The check used to test
# `*--dry-run*` against the WHOLE line, and its comment filter dropped only
# lines whose FIRST non-blank character is '#', so this classified as compliant.
test_dry_run_only_in_a_trailing_comment_is_rejected() {
  src="$(copy_source)"
  printf '          cm apply -v  # TODO put --dry-run back\n' \
    >>"$src/.github/workflows/ci.yml"

  export REPO="$src"
  assert_fail check-no-bare-apply.sh
  assert_out 'without --dry-run'
}

# NEGATIVE: a call site split over two lines with a backslash. The pattern is
# single-line, so neither half is a chezmoi apply on its own and the whole thing
# matched nothing.
test_a_backslash_continued_apply_is_rejected() {
  src="$(copy_source)"
  printf '          cm --source "$PWD" \\\n            apply -v\n' \
    >>"$src/.github/workflows/ci.yml"

  export REPO="$src"
  assert_fail check-no-bare-apply.sh
  assert_out 'without --dry-run'
}

# NEGATIVE: `apply` followed by a ';'. The old trailing class was
# `([[:space:]]|$)`, and ';' is neither.
test_an_apply_terminated_by_a_semicolon_is_rejected() {
  src="$(copy_source)"
  printf '          cm apply; echo done\n' >>"$src/.github/workflows/ci.yml"

  export REPO="$src"
  assert_fail check-no-bare-apply.sh
  assert_out 'without --dry-run'
}

# NEGATIVE: the test suite itself. It runs on the runner and on any laptop
# following README's "no network, no installs" promise, and it was not scanned
# at all — test_ci_config_in_home.sh already calls chezmoi outside cm(), so this
# was one word away from a real apply.
test_a_bare_apply_in_the_test_suite_is_rejected() {
  src="$(copy_source)"
  printf '\nchezmoi apply\n' >>"$src/.github/tests/test_meta.sh"

  export REPO="$src"
  assert_fail check-no-bare-apply.sh
  assert_out 'test_meta.sh'
  assert_out 'without --dry-run'
}

# NEGATIVE: a SECOND workflow file. Only ci.yml was scanned by name, so a new
# workflow was an unscanned place to put an apply.
test_a_bare_apply_in_another_workflow_is_rejected() {
  src="$(copy_source)"
  printf 'name: other\njobs:\n  a:\n    steps:\n      - run: chezmoi apply -v\n' \
    >"$src/.github/workflows/other.yml"

  export REPO="$src"
  assert_fail check-no-bare-apply.sh
  assert_out 'other.yml'
  assert_out 'without --dry-run'
}

# POSITIVE, and the counterweight to the four above: a real dry-run call site
# with a trailing comment is still fine. Comment stripping must not turn every
# annotated line into a violation.
test_a_dry_run_apply_with_a_trailing_comment_is_accepted() {
  src="$(copy_source)"
  printf '          cm apply --dry-run -v  # this one is fine\n' \
    >>"$src/.github/workflows/ci.yml"

  export REPO="$src"
  assert_ok check-no-bare-apply.sh
}

# A commented-out apply is not a call site. Without this the check would go off
# on the HARD RULE prose in ci.yml's own header.
test_a_commented_out_apply_is_not_flagged() {
  src="$(copy_source)"
  printf '          # chezmoi --source "$PWD" apply -v   <- never do this\n' \
    >>"$src/.github/workflows/ci.yml"

  export REPO="$src"
  assert_ok check-no-bare-apply.sh
}

# If the pattern ever stops matching, the check silently guards nothing. It has
# to fail loudly instead.
#
# Deleting check-apply-dry-run.sh alone is enough, and that is the point: this
# also pins the self-exclusion. check-no-bare-apply.sh's own error and report
# strings contain the words "chezmoi apply ... --dry-run", so before it learned
# to skip itself it counted two phantom call sites — which held `found` above
# zero and left this tripwire permanently disarmed.
test_check_fails_when_it_matches_no_call_sites() {
  src="$(copy_source)"
  rm -f "$src/.github/scripts/check-apply-dry-run.sh"

  export REPO="$src"
  assert_fail check-no-bare-apply.sh
  assert_out 'no longer guards anything'
}

# The count must be the real call sites and nothing else.
test_only_real_call_sites_are_counted() {
  assert_ok check-no-bare-apply.sh
  assert_not_out 'check-no-bare-apply.sh:'
}

run_tests
