#!/usr/bin/env bash
# The cycle-runner CronJob's invariants (SB-976).
#
# Every one of these was a hand-rolled mechanism in run.sh before the
# migration, and each is now a single line of YAML. A line of YAML is much
# easier to delete by accident than fifty lines of bash, and deleting one is
# silent: the CronJob still applies, still ticks, and the property is simply
# gone. That asymmetry is the reason this check exists.
#
# Text assertions, not a YAML parse: nothing here may depend on a python
# library being present on the runner, and there is exactly one CronJob in the
# file so there is no ambiguity about which block a key belongs to.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

DIR="$REPO/k3s/cycle-runner"
CJ="$DIR/cronjob.yaml"

for f in "$CJ" "$DIR/namespace.yaml" "$DIR/pvc.yaml" "$DIR/bootstrap.sh"; do
  [ -r "$f" ] || die "missing $f"
done

rc=0
bad() { err "$*"; rc=1; }

has() {  # PATTERN MESSAGE
  grep -qE -- "$1" "$CJ" || bad "$2"
}

# The replacement for run.sh's mkdir/pid lock. Without it two ticks run at
# once and there is nothing left in run.sh to stop them — the lock was removed
# in the same change precisely so there would not be two mechanisms.
has '^[[:space:]]*concurrencyPolicy:[[:space:]]*Forbid[[:space:]]*$' \
  "cronjob.yaml has no 'concurrencyPolicy: Forbid' — run.sh's lock was removed on the assumption this is here (SB-976)"

# SB-965: an unbounded `claude -p` held the lock for 10.5h and killed the loop
# overnight. The deadline has to be SHORTER than the schedule interval, or a
# killed run can still own the slot when the next tick is due.
deadline="$(sed -nE 's/^[[:space:]]*activeDeadlineSeconds:[[:space:]]*([0-9]+).*/\1/p' "$CJ" | head -1)"
if [ -z "$deadline" ]; then
  bad "cronjob.yaml has no activeDeadlineSeconds — SB-965's 10.5h stall has nothing stopping it"
else
  # "*/N * * * *" -> N minutes.
  every="$(sed -nE 's|^[[:space:]]*schedule:[[:space:]]*"\*/([0-9]+) .*|\1|p' "$CJ" | head -1)"
  if [ -z "$every" ]; then
    bad "could not read a '*/N' schedule out of cronjob.yaml — has the shape changed?"
  elif [ "$deadline" -ge $((every * 60)) ]; then
    bad "activeDeadlineSeconds=$deadline is not shorter than the ${every}m schedule — a killed run can still hold the slot when the next tick is due"
  else
    note "activeDeadlineSeconds=${deadline}s inside a ${every}m schedule"
  fi
fi

has '^[[:space:]]*startingDeadlineSeconds:[[:space:]]*[0-9]+[[:space:]]*$' \
  "cronjob.yaml has no startingDeadlineSeconds — a missed slot comes back late and collides, which is the launchd behaviour this replaces"

# A tick is not idempotent: it may already have opened a gate, posted to
# Telegram or pushed a branch. Retrying one duplicates those side effects.
has '^[[:space:]]*backoffLimit:[[:space:]]*0[[:space:]]*$' \
  "cronjob.yaml does not set 'backoffLimit: 0' — a retried tick duplicates gates, Telegram posts and pushes"

# SB-974: no `op` anywhere near a tick. The image has no binary, and nothing
# here may reintroduce a path to one.
# Comments are skipped: provision-cluster-secret.sh's header explains at length
# why it does NOT call `op`, and a guard that trips on its own rationale is a
# guard that gets deleted.
if hits="$(grep -vE '^[[:space:]]*#' "$DIR"/*.yaml "$DIR"/*.sh \
            | grep -nE '(^|[^a-zA-Z/-])op (read|item|signin)')"; then
  printf '%s\n' "$hits" >&2
  bad "a k3s/cycle-runner file invokes op — credentials come from the Secret, never 1Password at tick time (SB-974)"
fi

# Credentials as env vars and a mounted Secret, never literals.
has 'secretKeyRef' \
  "cronjob.yaml has no secretKeyRef — LINEAR_API_KEY and GH_TOKEN have to come from the Secret"
has '^[[:space:]]*secretName:[[:space:]]*cycle-runner[[:space:]]*$' \
  "cronjob.yaml does not mount the cycle-runner Secret — env.sh reads its three files from \$CYCLE_RUNNER_SECRETS_DIR"
has '^[[:space:]]*value:[[:space:]]*/secrets[[:space:]]*$' \
  "CYCLE_RUNNER_SECRETS_DIR is not pointed at the Secret mount — env.sh would look in \$HOME/.config/cycle-runner, which the pod does not have"

# The PVC is what makes gate state, the primary clones and the worktrees
# survive a pod restart. Without it every tick starts from nothing and
# `repo_dir_for_label` finds no checkout to cut a worktree from.
has '^[[:space:]]*claimName:[[:space:]]*cycle-runner-home[[:space:]]*$' \
  "cronjob.yaml does not mount the cycle-runner-home PVC — gate state, clones and worktrees would not survive the pod"

# The CronJob's image tag and the Dockerfile's CLAUDE_VERSION name the same
# thing in two files (SB-978). If they drift, the cluster silently runs a
# different claude from the one the repo's contract test was built against —
# and the contract is the whole reason the version is pinned at all.
dockerfile_ver="$(sed -n 's/^ARG CLAUDE_VERSION=\(.*\)$/\1/p' "$DIR/Dockerfile" | head -1)"
[ -n "$dockerfile_ver" ] || bad "could not read CLAUDE_VERSION out of the Dockerfile"

# Every image: line, so the initContainer cannot quietly diverge from the tick.
#
# The count is asserted first. This loop already went green while iterating
# ZERO times, because the extractor used \S — a GNU shorthand BSD sed does not
# know — so on macOS it matched nothing and the check passed on a manifest
# pinned to :latest. A loop over an empty list is indistinguishable from a
# loop that found nothing wrong.
n_images="$(grep -cE '^[[:space:]]*image:' "$CJ" || true)"
[ "${n_images:-0}" -ge 2 ] \
  || bad "found $n_images image: lines in cronjob.yaml — expected at least 2 (bootstrap + tick); has the extractor gone stale?"

while read -r img; do
  [ -z "$img" ] && continue
  case "$img" in
    *:latest)
      bad "cronjob.yaml uses '$img' — :latest is not revertible and gives doctor no version to read (SB-978)" ;;
    *":claude-$dockerfile_ver") ;;
    *)
      bad "cronjob.yaml runs '$img' but the Dockerfile pins CLAUDE_VERSION=$dockerfile_ver — the cluster would run a different claude from the one the contract test was built against" ;;
  esac
# [^[:space:]] rather than \S: BSD sed (macOS) does not know the shorthand and
# matches nothing, which made this loop iterate zero times and pass silently.
done < <(sed -nE 's|^[[:space:]]*image:[[:space:]]*([^[:space:]]+).*|\1|p' "$CJ")

# Anything that looks like a credential literal in a manifest is a hard stop.
if grep -nE '^[[:space:]]*(value|password|token):[[:space:]]*["'"'"']?(gh[pousr]_|sk-|xox|ey[JI])' "$DIR"/*.yaml; then
  bad "a credential literal appears in a k3s manifest — these are tracked in a PUBLIC repo"
fi

[ "$rc" -eq 0 ] || die "k3s/cycle-runner manifests do not hold their invariants"
note "cycle-runner CronJob holds its invariants (Forbid, deadlines, backoffLimit 0, Secret, PVC, no op)"
