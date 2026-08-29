#!/usr/bin/env bash
# check-skill-exceptions.sh — .chezmoiignore ignores .claude/skills/* wholesale,
# so a missing `!` exception makes a skill stop syncing with no error anywhere.
# That failure is invisible on the machine that authored the skill; it only
# shows up as an absence on the OTHER Mac, weeks later.
#
# Every negative below mutates a COPY of the source tree. The real repo is only
# ever read.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

test_all_four_skill_dirs_are_managed() {
  need_bin chezmoi
  assert_ok check-skill-exceptions.sh
  assert_out 'ok   linear-crud ('
  assert_out 'ok   todo ('
  assert_out 'ok   session-audit ('
  assert_out 'ok   backlog-groom ('
}

test_every_exception_line_has_a_source_dir() {
  need_bin chezmoi
  assert_ok check-skill-exceptions.sh
  assert_out 'ok   !linear-crud'
  assert_out 'ok   !todo'
  assert_out 'ok   !session-audit'
  assert_out 'ok   !backlog-groom'
}

# NEGATIVE: the exact mistake the README warns about — add a skill, forget the
# `!.claude/skills/<name>` line, and chezmoi add reports success while syncing
# nothing.
test_new_skill_without_an_exception_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  mkdir -p "$src/dot_claude/skills/brand-new-skill"
  printf '# new skill\n' >"$src/dot_claude/skills/brand-new-skill/SKILL.md"

  export REPO="$src"
  assert_fail check-skill-exceptions.sh
  assert_out "skill 'brand-new-skill' is not managed"
}

# NEGATIVE: a typo in the exception. This has to be caught in BOTH directions —
# the skill goes unmanaged, AND the exception dangles. Catching only one of the
# two lets a rename pass with the skill silently unsynced.
test_exception_typo_fails_in_both_directions() {
  need_bin chezmoi
  src="$(copy_source)"
  sed 's|^!\.claude/skills/todo$|!.claude/skills/todos|' "$src/.chezmoiignore" \
    >"$src/.chezmoiignore.new"
  mv "$src/.chezmoiignore.new" "$src/.chezmoiignore"
  grep -q '^!\.claude/skills/todos$' "$src/.chezmoiignore" \
    || fail "fixture did not apply the typo"

  export REPO="$src"
  assert_fail check-skill-exceptions.sh
  assert_out "skill 'todo' is not managed"
  assert_out "exception '!.claude/skills/todos' has no dot_claude/skills/todos directory"
}

# NEGATIVE: a skill deleted from the source tree but left in .chezmoiignore.
# Only the reverse direction fires here, which is what makes it worth its own
# test rather than folding it into the typo case.
test_stale_exception_for_a_deleted_skill_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  rm -rf "$src/dot_claude/skills/todo"

  export REPO="$src"
  assert_fail check-skill-exceptions.sh
  assert_out "exception '!.claude/skills/todo' has no dot_claude/skills/todo directory"
  assert_not_out "skill 'todo' is not managed"
}

# NEGATIVE: dropping the exception line but keeping the skill. This is the
# "do not simplify this file" case called out in .chezmoiignore's own header.
test_removing_an_exception_line_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  grep -v '^!\.claude/skills/session-audit$' "$src/.chezmoiignore" \
    >"$src/.chezmoiignore.new"
  mv "$src/.chezmoiignore.new" "$src/.chezmoiignore"

  export REPO="$src"
  assert_fail check-skill-exceptions.sh
  assert_out "skill 'session-audit' is not managed"
}

# The check asks chezmoi what it MANAGES rather than grepping .chezmoiignore,
# and it counts managed FILES rather than trusting the directory entry. This
# test is why it does both.
#
# Measured chezmoi v2.70.0 behaviour, which is not gitignore's:
#   - a later `.claude/skills/todo` line does NOT re-ignore a path that an
#     earlier `!` exception un-ignored (last-match-wins does not apply);
#   - but `.claude/skills/todo/**` DOES re-ignore the contents, while chezmoi
#     goes on listing the bare `.claude/skills/todo` directory entry.
#
# So the skill syncs nothing while still appearing in `chezmoi managed`. A
# check that grepped .chezmoiignore, and equally one that only looked for the
# directory entry, would both pass here.
test_exception_defeated_by_a_later_rule_is_still_caught() {
  need_bin chezmoi
  src="$(copy_source)"
  printf '\n.claude/skills/todo/**\n' >>"$src/.chezmoiignore"

  export REPO="$src"
  assert_fail check-skill-exceptions.sh
  assert_out "skill 'todo' is listed but NONE of its files are managed"
}

run_tests
