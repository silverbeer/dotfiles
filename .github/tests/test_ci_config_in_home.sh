#!/usr/bin/env bash
# shellcheck disable=SC2016  # fixtures are literal shell/jq source; expansion
#                             is exactly what must NOT happen here.
# check-no-ci-config-in-home.sh — a root-level source entry that is not
# dot-prefixed becomes a chezmoi TARGET. That is how ~/README.md got written to
# this user's home directory for months.
#
# The failure is silent by construction: the repo looks fine, CI is green, and
# the only symptom is junk appearing in $HOME on every machine at the next
# apply. So each negative below renames one file in a scratch copy and asserts
# the check notices.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

test_real_repo_deploys_no_ci_config() {
  need_bin chezmoi
  assert_ok check-no-ci-config-in-home.sh
  assert_out 'no CI config, docs or tests in the managed set'
}

# NEGATIVE: the exact "tidy-up" this guards against. .chezmoiignore lists the
# DOTTED name `.ruff.toml`, so it does not match `ruff.toml` and offers no
# protection at all here — chezmoi skipping dot-prefixed source entries is the
# only thing that was holding, and the rename removes it.
test_ruff_toml_renamed_without_the_dot_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  mv "$src/.ruff.toml" "$src/ruff.toml"

  export REPO="$src"
  assert_fail check-no-ci-config-in-home.sh
  assert_out 'ruff.toml'
  assert_out 'being deployed into $HOME'
}

test_gitleaks_toml_renamed_without_the_dot_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  mv "$src/.gitleaks.toml" "$src/gitleaks.toml"

  export REPO="$src"
  assert_fail check-no-ci-config-in-home.sh
  assert_out 'gitleaks.toml'
}

# NEGATIVE: the workflow directory itself. If .github ever loses its dot, CI
# config starts landing in $HOME on every machine.
test_github_dir_renamed_without_the_dot_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  mv "$src/.github" "$src/github"

  export REPO="$src"
  assert_fail check-no-ci-config-in-home.sh
  assert_out 'github'
}

# NEGATIVE: the documented historical regression. README.md and SETUP.md are
# not dot-prefixed, so ONLY the .chezmoiignore entries keep them out of $HOME.
# Deleting those lines must not be quiet.
test_readme_dropped_from_chezmoiignore_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  grep -vxE 'README\.md|SETUP\.md' "$src/.chezmoiignore" >"$src/.ci.new"
  mv "$src/.ci.new" "$src/.chezmoiignore"

  export REPO="$src"
  assert_fail check-no-ci-config-in-home.sh
  assert_out 'README.md'
}

# The tests directory lives under .github precisely so it inherits that
# protection. Assert it, rather than assuming it.
test_tests_dir_is_not_a_chezmoi_target() {
  need_bin chezmoi
  work="$(new_dir)"
  dest="$work/home"; mkdir -p "$dest"
  HOME="$dest" chezmoi --source "$REPO_ROOT" --destination "$dest" --no-tty managed \
    >"$work/managed.txt"
  if grep -qE '(^|/)(tests|harness\.sh|run\.sh)' "$work/managed.txt"; then
    fail "the test suite is being deployed into \$HOME:
$(grep -nE '(^|/)(tests|harness\.sh|run\.sh)' "$work/managed.txt")"
  fi
}

run_tests
