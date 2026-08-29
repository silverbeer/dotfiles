#!/usr/bin/env bash
# shellcheck disable=SC2016  # fixtures are literal shell/jq source; expansion
#                             is exactly what must NOT happen here.
# check-modify-settings.sh — dot_claude/modify_settings.json.tmpl is the one
# piece of genuinely tricky logic in this repo. Claude Code rewrites
# ~/.claude/settings.json at runtime, so the script has to merge rather than
# overwrite: enforce hooks/statusLine, union permissions.allow, seed defaults,
# and preserve everything it has never heard of.
#
# The positives below are the contract. The negatives break the template in a
# scratch copy and assert the check notices — a merge test that only ever sees
# a correct template proves nothing about the test.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

# --------------------------------------------------------------- positives

test_merge_preserves_runtime_keys_and_unions_the_allow_list() {
  need_bin chezmoi jq
  assert_ok check-modify-settings.sh
  assert_out "ok   .tui.diffMode"                                      # unknown runtime key survives
  assert_out "ok   .theme"                                             # live value beats the seeded default
  assert_out "ok   .skipWorkflowUsageWarning"
  assert_out 'ok   .permissions.allow | index("Bash(gh api:*)") != null'   # machine-added permission kept
  assert_out 'ok   .permissions.allow | index("Bash(linear:*)") != null'   # canonical permission added
  assert_out 'ok   .permissions.allow | index("Bash") != null'
}

test_hooks_and_status_line_are_enforced_not_merged() {
  need_bin chezmoi jq
  assert_ok check-modify-settings.sh
  assert_out "ok   .hooks.PreToolUse[0].hooks[0].command"
  assert_out "ok   .statusLine.command"
}

test_merge_is_idempotent_and_seeds_an_empty_home() {
  need_bin chezmoi jq
  assert_ok check-modify-settings.sh
  assert_out "idempotent"
  assert_not_out "not idempotent"
}

# --------------------------------------------------------------- negatives

# NEGATIVE: replace the union with a plain assignment. This is the most likely
# real regression — it looks like a simplification, `chezmoi apply` still exits
# 0, and the only symptom is that every machine-specific permission silently
# disappears on the next apply.
test_clobbering_the_allow_list_is_caught() {
  need_bin chezmoi jq
  src="$(copy_source)"
  tmpl="$src/dot_claude/modify_settings.json.tmpl"
  # awk, not sed: the line is full of |, $ and () and every sed delimiter is
  # already taken by the jq syntax being edited.
  awk '{ if (index($0, ".permissions.allow = ($have")) \
           print "  | .permissions.allow = $can.permissions.allow"; \
         else print }' "$tmpl" >"$tmpl.new"
  mv "$tmpl.new" "$tmpl"
  grep -q 'allow = \$can\.permissions\.allow' "$tmpl" \
    || fail "fixture did not replace the union"

  export REPO="$src"
  assert_fail check-modify-settings.sh
  assert_out 'index("Bash(gh api:*)") != null'
  assert_out "expected 'true'"
}

# NEGATIVE: drop the `.hooks = $can.hooks` enforcement, so the live (stale)
# hook wins. The seed in the check sets PreToolUse to an empty array, which is
# exactly what a machine looks like after Claude Code has rewritten the file.
test_not_enforcing_the_rtk_hook_is_caught() {
  need_bin chezmoi jq
  src="$(copy_source)"
  tmpl="$src/dot_claude/modify_settings.json.tmpl"
  grep -v '\.hooks = \$can\.hooks' "$tmpl" >"$tmpl.new"
  mv "$tmpl.new" "$tmpl"

  export REPO="$src"
  assert_fail check-modify-settings.sh
  assert_out '.hooks.PreToolUse[0].hooks[0].command'
}

# NEGATIVE: make the template non-idempotent. A merge that produces a different
# result on the second run means `chezmoi apply` reports a diff forever, which
# trains everyone to ignore apply output.
test_a_non_idempotent_template_is_caught() {
  need_bin chezmoi jq
  src="$(copy_source)"
  tmpl="$src/dot_claude/modify_settings.json.tmpl"
  # Add a unique element on every run. This has to go INSIDE the jq program —
  # the template's last line is the closing quote — so insert above it. Every
  # other assertion still holds, so only the idempotency comparison catches it.
  awk 'BEGIN { q = sprintf("%c", 39) }
       $0 == q && !done { print "  | .permissions.allow += [\"drift-\" + (now|tostring)]"; done = 1 }
       { print }' "$tmpl" >"$tmpl.new"
  mv "$tmpl.new" "$tmpl"
  grep -q 'drift-' "$tmpl" || fail "fixture did not inject the drifting filter"

  export REPO="$src"
  assert_fail check-modify-settings.sh
  assert_out 'is not idempotent'
}

# NEGATIVE: emit something that is not JSON at all.
test_invalid_json_output_is_caught() {
  need_bin chezmoi jq
  src="$(copy_source)"
  tmpl="$src/dot_claude/modify_settings.json.tmpl"
  printf '\nprintf "trailing garbage\\n"\n' >>"$tmpl"

  export REPO="$src"
  assert_fail check-modify-settings.sh
  assert_out 'not valid JSON'
}

run_tests
