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

# .github/tests lives under .github precisely so it inherits that protection.
# Assert it, rather than assuming it. harness.sh and run.sh exist nowhere else
# in the repo, so a bare basename USED TO BE an unambiguous marker of THIS
# directory leaking — until SB-929 gave a legitimately-managed skill script
# the same basename (dot_claude/skills/cycle-runner/scripts/run.sh, deployed
# to .claude/skills/cycle-runner/scripts/run.sh, which is supposed to be
# there). The marker has to be the full `.github/tests/` prefix now, not just
# the filename — unlike a bare "tests" path segment, which was already not
# unique to it: a skill's own tests/ (dot_claude/skills/gatekeeper/tests,
# SB-508) is a legitimate, intentionally-managed deliverable, not a leak.
test_github_tests_dir_is_not_a_chezmoi_target() {
  need_bin chezmoi
  work="$(new_dir)"
  dest="$work/home"; mkdir -p "$dest"
  HOME="$dest" chezmoi --source "$REPO_ROOT" --destination "$dest" --no-tty managed \
    >"$work/managed.txt"
  if grep -qE '(^|/)\.github/tests/(harness\.sh|run\.sh)$' "$work/managed.txt"; then
    fail "the .github test suite is being deployed into \$HOME:
$(grep -nE '(^|/)\.github/tests/(harness\.sh|run\.sh)$' "$work/managed.txt")"
  fi
}

# ...and the complement: a bare root-level tests/ WOULD be a leak (it is what
# check-no-ci-config-in-home.sh's own `^tests(/|$)` guards), unlike a nested
# skill tests/ directory, which is fine.
test_a_root_level_tests_dir_would_be_rejected_by_the_check() {
  need_bin chezmoi
  src="$(copy_source)"
  mv "$src/.github/tests" "$src/tests"
  rmdir "$src/.github" 2>/dev/null || true

  export REPO="$src"
  assert_fail check-no-ci-config-in-home.sh
  assert_out 'tests'
}

# NEGATIVE: k3s/ is not dot-prefixed, so its .chezmoiignore entry is the ONLY
# thing between the cluster manifests and a ~/k3s on every machine. Same shape
# as README.md, and the entry was added to .chezmoiignore before the directory
# existed for exactly this reason (SB-975).
test_k3s_dropped_from_chezmoiignore_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  grep -vx 'k3s' "$src/.chezmoiignore" >"$src/.ci.new"
  mv "$src/.ci.new" "$src/.chezmoiignore"
  grep -qx 'k3s' "$src/.chezmoiignore" && fail "fixture did not drop the k3s entry"

  export REPO="$src"
  assert_fail check-no-ci-config-in-home.sh
  assert_out 'k3s'
}

run_tests
