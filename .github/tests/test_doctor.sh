#!/usr/bin/env bash
# check-doctor.sh — offline tests for doctor.sh's cycle-runner + chezmoi-sync
# checks (SB-930). The positive case proves the check passes on the current
# tree; each negative breaks ONE thing in a copy of the tree and asserts the
# check fails naming it.
# shellcheck disable=SC2016  # the edit()/grep fixtures quote doctor.sh's $-names literally, on purpose
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

DOCTOR=dot_claude/skills/linear-crud/scripts/executable_doctor.sh

# sed -i differs between BSD and GNU; write to a sibling and move.
edit() { sed "$1" "$2" >"$2.new" && mv "$2.new" "$2"; }

test_the_check_passes_on_the_current_tree() {
  need_bin bash python3
  src="$(copy_source)"
  export REPO="$src"
  assert_ok check-doctor.sh
  assert_out 'ok   token unset -> warns'
  assert_out 'ok   token set + claude succeeds -> ok'
  assert_out 'ok   token set + claude fails -> fail'
  assert_out 'ok   off-mini -> skips the claude -p check'
  assert_out 'ok   off-mini + token set -> still never calls claude'
  assert_out 'ok   gh auth status ok'
  assert_out 'ok   gh auth status fails -> warn'
  assert_out "ok   gatekeeper skill absent -> warns 'skill not found'"
  assert_out 'ok   GATEKEEPER_TG_TOKEN unset (skill present) -> skips cleanly'
  assert_out 'ok   no lock dir at all -> ok'
  assert_out "ok   lock held by a running pid"
  assert_out "ok   lock names a dead pid -> warn"
  assert_out 'ok   on-mini + plist absent -> fail'
  assert_out 'ok   on-mini + plist present, not loaded -> ok (present) + warn'
  assert_out 'ok   on-mini + plist present, loaded -> ok, ok'
  assert_out 'ok   off-mini -> skips the plist check'
  assert_out 'ok   chezmoi status clean'
  assert_out "ok   chezmoi status dirty -> fail, names the PR #37/#35 incident"
  assert_out 'ok   chezmoi status itself errors'
  assert_out 'check-doctor: all offline tests passed'
}

# NEGATIVE: doctor.sh itself is missing (e.g. a rename that broke the path
# this ticket's checks assume). Must fail loudly, not silently report nothing.
test_missing_doctor_script_fails() {
  need_bin bash python3
  src="$(copy_source)"
  rm -f "$src/$DOCTOR"
  export REPO="$src"
  assert_fail check-doctor.sh
  assert_out "missing $src/$DOCTOR"
}

# NEGATIVE: the token-unset warn text drifts (e.g. someone rewords it without
# updating the machine-readable contract this check greps for) — this is the
# exact string a human on a non-mini laptop reads to know cycle-runner needs
# no action from them; losing it silently must not go unnoticed.
test_reworded_oauth_warning_fails() {
  need_bin bash python3
  src="$(copy_source)"
  edit 's/CLAUDE_CODE_OAUTH_TOKEN not set/no oauth token configured/' "$src/$DOCTOR"
  grep -q 'no oauth token configured' "$src/$DOCTOR" || fail "fixture did not reword the warning"
  export REPO="$src"
  assert_fail check-doctor.sh
  assert_out 'token-unset case did not warn as expected'
}

# NEGATIVE: the claude bounded-check flags regress back to the exact mistake
# SB-929 already made once (a nonexistent --max-turns), which would make
# every real invocation on the mini fail outright. Catch it at the shell
# level: the claude stub only recognises the flags this ticket verified
# actually exist, so a --max-turns re-add makes the stub's own "ok, all
# good" branch stop matching and the args echo back instead.
test_max_turns_regression_fails() {
  need_bin bash python3
  src="$(copy_source)"
  edit 's/--tools "" --max-budget-usd 0.02/--tools "" --max-turns 1/' "$src/$DOCTOR"
  grep -q -- '--max-turns 1' "$src/$DOCTOR" || fail "fixture did not reintroduce --max-turns"
  export REPO="$src"
  assert_fail check-doctor.sh
  assert_out 'token-set-success case did not report as expected'
}

# NEGATIVE: the hostname gate is defeated (e.g. someone "simplifies" the
# `on_mini` computation to always-true) — the SB-930 review's actual
# complaint: a laptop with vault access would then run the real,
# billed `claude -p` check (and, separately, query launchd for a plist that
# was never deployed there) on every doctor.sh run.
test_hostname_gate_defeated_fails() {
  need_bin bash python3
  src="$(copy_source)"
  edit 's/\[ "\$CURRENT_HOSTNAME" = "\$CYCLE_RUNNER_HOST" \] \&\& on_mini=1/on_mini=1/' "$src/$DOCTOR"
  grep -q '^on_mini=1$' "$src/$DOCTOR" || fail "fixture did not defeat the hostname gate"
  export REPO="$src"
  assert_fail check-doctor.sh
  assert_out 'off-mini case did not skip as expected'
}

# NEGATIVE: the gatekeeper skill-presence check is defeated, reintroducing
# the exact unreachable-branch bug the review found: with the skill absent,
# GATEKEEPER_TG_TOKEN is also unset (env.sh never sourced), so the token
# branch fires first and "gatekeeper skill not found" can never be reported.
test_gatekeeper_skill_check_reorder_regresses_fails() {
  need_bin bash python3
  src="$(copy_source)"
  edit 's/if \[ -z "\$GK_SCRIPTS" \]; then/if false; then/' "$src/$DOCTOR"
  export REPO="$src"
  assert_fail check-doctor.sh
  assert_out 'gatekeeper-skill-absent case did not report as expected'
}

# NEGATIVE: the chezmoi dirty-state message loses the PR #37/#35 callout —
# this is the actual root-cause fix the ticket exists to make, so silently
# dropping it must fail the suite, not just this one doctor check.
test_dropped_pr37_pr35_reference_fails() {
  need_bin bash python3
  src="$(copy_source)"
  edit 's/ (this happened: PR #37 silently reverted PR #35)//' "$src/$DOCTOR"
  grep -q 'this happened: PR #37' "$src/$DOCTOR" \
    && fail "fixture did not drop the PR #37/#35 reference"
  export REPO="$src"
  assert_fail check-doctor.sh
  assert_out "chezmoi-dirty case did not report as expected"
}

# NEGATIVE: the stale-lock staleness test (kill -0) is defeated so an
# abandoned lock reads as "still running" forever — the same class of bug
# check-cycle-runner-policy.sh already guards in run.sh's own lock code, but
# doctor.sh's INFORMATIONAL read of the same lock file is separate code and
# needs its own coverage.
test_broken_lock_staleness_check_fails() {
  need_bin bash python3
  src="$(copy_source)"
  edit 's|kill -0 "\$lock_pid" 2>/dev/null|true|' "$src/$DOCTOR"
  grep -q 'if \[ -n "\$lock_pid" \] && true; then' "$src/$DOCTOR" \
    || fail "fixture did not defeat the staleness check"
  export REPO="$src"
  assert_fail check-doctor.sh
  assert_out "stale-lock case did not report as expected"
}

run_tests
