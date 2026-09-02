#!/usr/bin/env bash
# check-claude-cli-contract.sh — the runner's `claude` invocations and
# k3s/cycle-runner/claude-cli-contract.sh must not drift apart.
#
# The contract script itself is checked at IMAGE BUILD time, against a real
# `claude --help`. That direction is covered by the Docker build. What it
# cannot see is a flag ADDED to run.sh and never added to the contract: the
# build stays green, the flag is unguarded, and the next `claude` release that
# renames it takes the runner down unannounced. These tests cover that
# direction, which is the one with no other safety net.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

CONTRACT=k3s/cycle-runner/claude-cli-contract.sh
RUN_SH=dot_claude/skills/cycle-runner/scripts/run.sh

test_real_repo_contract_covers_every_flag_the_runner_passes() {
  assert_ok check-claude-cli-contract.sh
  assert_out 'contract covers all'
}

# NEGATIVE: the whole point. A new flag on a real invocation, not added to
# REQUIRED_FLAGS, must fail.
test_a_new_claude_flag_missing_from_the_contract_is_rejected() {
  src="$(copy_source)"
  perl -0pi -e 's/(--permission-mode acceptEdits)/--fallback-model haiku $1/' "$src/$RUN_SH"
  grep -q -- '--fallback-model' "$src/$RUN_SH" || fail "fixture did not inject the flag"

  export REPO="$src"
  assert_fail check-claude-cli-contract.sh
  assert_out '--fallback-model'
  assert_out 'not in the contract'
}

# NEGATIVE: dropping a flag FROM the contract while the runner still passes it
# is the same drift from the other side.
test_removing_a_flag_from_required_flags_is_rejected() {
  src="$(copy_source)"
  grep -vx '  --resume' "$src/$CONTRACT" >"$src/.c.new"
  mv "$src/.c.new" "$src/$CONTRACT"
  grep -q -- '^  --resume$' "$src/$CONTRACT" && fail "fixture did not remove --resume"

  export REPO="$src"
  assert_fail check-claude-cli-contract.sh
  assert_out '--resume'
}

# REGRESSION: `grep -o` returns no overlapping matches, so a pattern that also
# consumed the TRAILING separator ate the space the next flag needed as its
# own leading boundary. On `claude -p --resume "$id"` that matched `-p` and
# silently dropped `--resume` — the check reported "covers all 7 flags" and
# was green while the contract's most important flag went unchecked.
#
# Asserting the count is what catches it: the failure mode is a check that
# still passes, so no assert_fail can see it.
test_two_flags_separated_by_a_single_space_are_both_extracted() {
  assert_ok check-claude-cli-contract.sh
  assert_out 'all 8 flags'
}

# The prompt argument carries the SLASH COMMAND's own options —
# `-p "/triage --session-id X --run-id Y"`. --run-id is parsed by the skill,
# not by `claude`, and demanding the contract cover it is a false failure. A
# check that cries wolf gets deleted, so this is load-bearing.
test_slash_command_options_inside_the_prompt_are_not_treated_as_cli_flags() {
  assert_ok check-claude-cli-contract.sh
  assert_not_out 'run-id'
}

# The check greps the runner for `claude` invocations. If that grep ever stops
# matching — a rename, a wrapper function, a reformat — it would find nothing,
# report success, and check nothing at all. It must die instead.
test_a_grep_that_finds_no_invocations_fails_rather_than_passing_vacuously() {
  src="$(copy_source)"
  for f in "$src/dot_claude/skills/cycle-runner/scripts"/*.sh; do
    perl -0pi -e 's/\bclaude\b(\s+-)/CLAUDE_BIN$1/g' "$f"
  done

  export REPO="$src"
  assert_fail check-claude-cli-contract.sh
  assert_out 'no claude invocations'
}

test_a_missing_contract_script_fails_loudly() {
  src="$(copy_source)"
  rm -f "$src/$CONTRACT"

  export REPO="$src"
  assert_fail check-claude-cli-contract.sh
  assert_out 'no contract script'
}

run_tests
