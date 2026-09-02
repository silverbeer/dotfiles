#!/usr/bin/env bash
# check-k3s-manifests.sh — the CronJob's invariants.
#
# Each of these was fifty lines of bash in run.sh before SB-976 and is now one
# line of YAML. That is the point of the migration and also its risk: a line of
# YAML is far easier to delete by accident, and deleting one is silent — the
# CronJob still applies, still ticks, and the property is simply gone.
#
# So every negative below removes exactly one line and asserts the check
# notices.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

# Same one-liner the other test files carry; harness.sh deliberately has no
# fixture-editing helpers of its own.
edit() { sed "$1" "$2" >"$2.new" && mv "$2.new" "$2"; }

CJ=k3s/cycle-runner/cronjob.yaml

test_the_real_manifests_hold_their_invariants() {
  assert_ok check-k3s-manifests.sh
  assert_out 'holds its invariants'
}

# NEGATIVE: the one that matters most. run.sh's mkdir/pid lock was deleted in
# the same change on the assumption this line is here; without it two ticks run
# at once and nothing anywhere stops them.
test_dropping_concurrency_policy_is_rejected() {
  src="$(copy_source)"
  grep -v 'concurrencyPolicy: Forbid' "$src/$CJ" >"$src/.cj" && mv "$src/.cj" "$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'concurrencyPolicy'
}

# NEGATIVE: SB-965 — an unbounded `claude -p` held the lock 10.5h and killed
# the loop overnight. The deadline is the only thing standing in for that now.
test_dropping_the_active_deadline_is_rejected() {
  src="$(copy_source)"
  grep -v 'activeDeadlineSeconds' "$src/$CJ" >"$src/.cj" && mv "$src/.cj" "$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out '10.5h stall'
}

# NEGATIVE: a deadline LONGER than the schedule interval is worse than useless
# — a killed run can still own the slot when the next tick is due, which is
# exactly the 21:40-still-running-at-08:05 shape launchd had.
test_a_deadline_longer_than_the_schedule_is_rejected() {
  src="$(copy_source)"
  edit 's/activeDeadlineSeconds: 1500/activeDeadlineSeconds: 5400/' "$src/$CJ"
  grep -q 'activeDeadlineSeconds: 5400' "$src/$CJ" || fail "fixture did not widen the deadline"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'not shorter than the 30m schedule'
}

# NEGATIVE: a tick is not idempotent — it may already have opened a gate,
# posted to Telegram or pushed a branch. Retrying one duplicates all of that.
test_a_nonzero_backoff_limit_is_rejected() {
  src="$(copy_source)"
  edit 's/backoffLimit: 0/backoffLimit: 3/' "$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'duplicates gates'
}

# NEGATIVE: SB-974. The image has no `op` binary; nothing here may reintroduce
# a path to one.
test_an_op_invocation_in_a_manifest_or_script_is_rejected() {
  src="$(copy_source)"
  printf '\nop read op://agents/cycle-runner-claude/token\n' >>"$src/k3s/cycle-runner/bootstrap.sh"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'invokes op'
}

# ...and the complement: the check must NOT trip on the comments that explain
# why `op` is absent. provision-cluster-secret.sh's header discusses `op read`
# at length, and a guard that fails on its own rationale gets deleted.
test_op_mentioned_only_in_a_comment_is_allowed() {
  src="$(copy_source)"
  printf '\n# op read is deliberately not called here — see SB-974.\n' \
    >>"$src/k3s/cycle-runner/bootstrap.sh"
  export REPO="$src"
  assert_ok check-k3s-manifests.sh
}

# NEGATIVE: the secrets mount path. env.sh reads $CYCLE_RUNNER_SECRETS_DIR and
# falls back to $HOME/.config/cycle-runner — a directory the pod does not have,
# so the failure would be a silent no-op and then a missing-token error much
# later.
test_dropping_the_secrets_mount_path_is_rejected() {
  src="$(copy_source)"
  edit 's|value: /secrets|value: /nowhere|' "$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'CYCLE_RUNNER_SECRETS_DIR'
}

# NEGATIVE: no PVC means gate state, the primary clones and the worktrees all
# vanish with the pod, and `repo_dir_for_label` finds nothing to cut from.
test_dropping_the_pvc_is_rejected() {
  src="$(copy_source)"
  edit 's/claimName: cycle-runner-home/claimName: something-else/' "$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'PVC'
}

# NEGATIVE: this repo is PUBLIC. A token pasted into a manifest "just to test
# it" must not reach a commit.
test_a_credential_literal_in_a_manifest_is_rejected() {
  src="$(copy_source)"
  # Assembled at runtime so no credential-shaped literal exists in this repo —
  # same reason test_meta.sh asserts there are none.
  printf '  value: "%s_%s"\n' "gh$(printf 'p')" "0123456789abcdef" >>"$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'credential literal'
}

test_a_missing_manifest_fails_loudly() {
  src="$(copy_source)"
  rm -f "$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'missing'
}

# NEGATIVE: :latest. It is not revertible — SB-966 asked that a version bump
# "open a PR rather than push to main, so it is visible and revertible", and
# with :latest there is nothing to revert TO. It also leaves doctor.sh no
# version to compare against the mini's own claude.
test_a_latest_image_tag_is_rejected() {
  src="$(copy_source)"
  edit 's|cycle-runner:claude-[0-9.]*|cycle-runner:latest|g' "$src/$CJ"
  grep -q 'cycle-runner:latest' "$src/$CJ" || fail "fixture did not set :latest"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'not revertible'
}

# NEGATIVE: the image tag and the Dockerfile ARG drift. The cluster would then
# run a different claude from the one the build-time CLI contract was checked
# against, which is the entire reason the version is pinned.
test_an_image_tag_that_disagrees_with_the_dockerfile_is_rejected() {
  src="$(copy_source)"
  edit 's|cycle-runner:claude-[0-9.]*|cycle-runner:claude-9.9.9|g' "$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'different claude'
}

# REGRESSION: the loop above went green while iterating ZERO times — the
# extractor used \S, which BSD sed does not know, so on macOS it matched
# nothing and a :latest manifest passed. A loop over an empty list looks
# exactly like a loop that found nothing wrong, so the COUNT is what catches
# it.
test_a_manifest_with_no_image_lines_fails_rather_than_passing_vacuously() {
  src="$(copy_source)"
  grep -v '^[[:space:]]*image:' "$src/$CJ" >"$src/.cj" && mv "$src/.cj" "$src/$CJ"
  export REPO="$src"
  assert_fail check-k3s-manifests.sh
  assert_out 'image: lines'
}

run_tests
