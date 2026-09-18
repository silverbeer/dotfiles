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
LS="$DIR/listener.yaml"
PC="$DIR/po-chat.yaml"

for f in "$CJ" "$LS" "$PC" "$DIR/namespace.yaml" "$DIR/pvc.yaml" "$DIR/bootstrap.sh"; do
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

# Every image: line in every manifest, so neither the initContainer, the
# listener nor the PO chat can quietly diverge from the tick.
#
# The count is asserted first. This loop already went green while iterating
# ZERO times, because the extractor used \S — a GNU shorthand BSD sed does not
# know — so on macOS it matched nothing and the check passed on a manifest
# pinned to :latest. A loop over an empty list is indistinguishable from a
# loop that found nothing wrong.
n_images="$(grep -cE '^[[:space:]]*image:' "$CJ" || true)"
[ "${n_images:-0}" -ge 2 ] \
  || bad "found $n_images image: lines in cronjob.yaml — expected at least 2 (bootstrap + tick); has the extractor gone stale?"
n_images="$(grep -cE '^[[:space:]]*image:' "$LS" || true)"
[ "${n_images:-0}" -ge 1 ] \
  || bad "found $n_images image: lines in listener.yaml — expected at least 1; has the extractor gone stale?"
n_images="$(grep -cE '^[[:space:]]*image:' "$PC" || true)"
[ "${n_images:-0}" -ge 1 ] \
  || bad "found $n_images image: lines in po-chat.yaml — expected at least 1; has the extractor gone stale?"

for manifest in "$CJ" "$LS" "$PC"; do
  name="$(basename "$manifest")"
  while read -r img; do
    [ -z "$img" ] && continue
    case "$img" in
      *:latest)
        bad "$name uses '$img' — :latest is not revertible and gives doctor no version to read (SB-978)" ;;
      *":claude-$dockerfile_ver") ;;
      *)
        bad "$name runs '$img' but the Dockerfile pins CLAUDE_VERSION=$dockerfile_ver — the cluster would run a different claude from the one the contract test was built against" ;;
    esac
  # [^[:space:]] rather than \S: BSD sed (macOS) does not know the shorthand and
  # matches nothing, which made this loop iterate zero times and pass silently.
  done < <(sed -nE 's|^[[:space:]]*image:[[:space:]]*([^[:space:]]+).*|\1|p' "$manifest")
done

# ------------------------------------------------ the listener (SB-951)
#
# Telegram allows ONE getUpdates reader per bot token. The listener Deployment
# is that reader, and two lines of its YAML are what keep it to one: delete
# either and it still applies, still runs, and 409s the day a rollout or a
# scale-up overlaps two pods.
ls_has() {  # PATTERN MESSAGE
  grep -qE -- "$1" "$LS" || bad "$2"
}

ls_has '^[[:space:]]*replicas:[[:space:]]*1[[:space:]]*$' \
  "listener.yaml does not set 'replicas: 1' — two listener pods are two getUpdates readers, a 409 for both (SB-951)"
ls_has '^[[:space:]]*type:[[:space:]]*Recreate[[:space:]]*$' \
  "listener.yaml has no 'strategy: type: Recreate' — a rolling update starts the new pod before stopping the old one: two getUpdates readers, a 409 on every rollout (SB-951)"

# Same state as the runner — the shared gate files ARE the handoff — and the
# same credentials mechanism.
ls_has '^[[:space:]]*claimName:[[:space:]]*cycle-runner-home[[:space:]]*$' \
  "listener.yaml does not mount the cycle-runner-home PVC — decisions it records would never reach the runner"
ls_has '^[[:space:]]*secretName:[[:space:]]*cycle-runner[[:space:]]*$' \
  "listener.yaml does not mount the cycle-runner Secret — gatekeeper/env.sh reads the Telegram token from it"
ls_has '^[[:space:]]*value:[[:space:]]*/secrets[[:space:]]*$' \
  "listener.yaml does not point CYCLE_RUNNER_SECRETS_DIR at the Secret mount — env.sh would find no Telegram token"

# SB-981: the node is ~95% requested. An always-on pod with no cpu request is
# counted by the scheduler as using none, which on a node this full is the
# arithmetic that left the last pod Pending.
ls_has '^[[:space:]]*cpu:[[:space:]]*[0-9]+m?[[:space:]]*$' \
  "listener.yaml has no cpu request — an always-on pod needs a real one on a node this full (SB-981)"

# The listener runs no claude and pushes nothing; an always-on pod holds the
# fewest credentials it can.
if grep -vE '^[[:space:]]*#' "$LS" | grep -qE '(key|path):[[:space:]]*(claude-token|gh-token)[[:space:]]*$'; then
  bad "listener.yaml mounts claude-token or gh-token — the listener needs only the two Telegram files and the Linear key"
fi

# ------------------------------------------------ the PO chat (SB-1089)
#
# One consumer of the inbox, with single-writer state (pending proposals,
# sessions, the budget ledger), running claude in a config dir of its own so no
# interactive plugin leaks in (SB-991).
pc_has() {  # PATTERN MESSAGE
  grep -qE -- "$1" "$PC" || bad "$2"
}

pc_has '^[[:space:]]*replicas:[[:space:]]*1[[:space:]]*$' \
  "po-chat.yaml does not set 'replicas: 1' — two chat pods race on pending.json, the sessions and the budget ledger (SB-1089)"
pc_has '^[[:space:]]*type:[[:space:]]*Recreate[[:space:]]*$' \
  "po-chat.yaml has no 'strategy: type: Recreate' — a rolling update runs two chat pods over the same pending proposal (SB-1089)"
pc_has '^[[:space:]]*claimName:[[:space:]]*cycle-runner-home[[:space:]]*$' \
  "po-chat.yaml does not mount the cycle-runner-home PVC — it would never see the inbox the listener writes"
pc_has '^[[:space:]]*secretName:[[:space:]]*cycle-runner[[:space:]]*$' \
  "po-chat.yaml does not mount the cycle-runner Secret — env.sh would find no claude or Telegram token"
pc_has '^[[:space:]]*value:[[:space:]]*/secrets[[:space:]]*$' \
  "po-chat.yaml does not point CYCLE_RUNNER_SECRETS_DIR at the Secret mount — env.sh would find no claude or Telegram token"
pc_has '^[[:space:]]*-[[:space:]]*name:[[:space:]]*CLAUDE_CONFIG_DIR[[:space:]]*$' \
  "po-chat.yaml does not set CLAUDE_CONFIG_DIR — claude would load the runner's ~/.claude, and interactive plugins leak into the chat (SB-991)"
pc_has '^[[:space:]]*cpu:[[:space:]]*[0-9]+m?[[:space:]]*$' \
  "po-chat.yaml has no cpu request — an always-on pod needs a real one on a node this full (SB-981)"
# The memory limit sits under `limits:`, the only memory line after it.
if ! sed -n '/^[[:space:]]*limits:[[:space:]]*$/,$p' "$PC" | grep -qE '^[[:space:]]*memory:[[:space:]]*[0-9]+[MG]i[[:space:]]*$'; then
  bad "po-chat.yaml has no memory limit — a leak in a pod that runs claude all day should kill the pod, not the node"
fi

# Exactly the three files it reads: claude-token for the PO, the two Telegram
# files for replies. Never gh-token: the chat pushes nothing.
pc_items="$(grep -vE '^[[:space:]]*#' "$PC" | sed -nE 's/^[[:space:]]*-[[:space:]]*key:[[:space:]]*([^[:space:]]+).*/\1/p' | sort | tr '\n' ' ')"
if [ "$pc_items" != "claude-token telegram-chat-id telegram-token " ]; then
  bad "po-chat.yaml mounts Secret items '${pc_items% }' — expected exactly claude-token, telegram-token and telegram-chat-id, and never gh-token"
fi

# Anything that looks like a credential literal in a manifest is a hard stop.
if grep -nE '^[[:space:]]*(value|password|token):[[:space:]]*["'"'"']?(gh[pousr]_|sk-|xox|ey[JI])' "$DIR"/*.yaml; then
  bad "a credential literal appears in a k3s manifest — these are tracked in a PUBLIC repo"
fi

# SB-1095: the image's ENTRYPOINT is tini, so pid 1 reaps what `claude` and git
# spawn. A container `command:` REPLACES the ENTRYPOINT, which silently drops
# tini — the CronJob ran without a reaper from SB-976 until this check. Every
# container command in every manifest here must name tini first.
for m in "$DIR"/*.yaml; do
  # Print the first two list items after each `command:` key, one per line.
  firsts="$(awk '
    /^[[:space:]]*#/ { next }
    # A probe `exec: command:` runs inside the container; only container commands replace the ENTRYPOINT.
    /^[[:space:]]*command:[[:space:]]*$/ { if (prev !~ /exec:[[:space:]]*$/) { want = 2; items = "" }; prev = $0; next }
    want > 0 && /^[[:space:]]*- / {
      sub(/^[[:space:]]*- /, ""); items = items (items == "" ? "" : " ") $0
      if (--want == 0) print items
      prev = $0; next
    }
    { prev = $0 }
  ' "$m")"
  [ -n "$firsts" ] || continue
  while IFS= read -r first; do
    [ "$first" = "/usr/bin/tini --" ] \
      || bad "$(basename -- "$m") has a container command starting '$first' — it replaces the image ENTRYPOINT, so tini must come first ('/usr/bin/tini', '--') or pid 1 reaps nothing (SB-1095)"
  done <<<"$firsts"
done

[ "$rc" -eq 0 ] || die "k3s/cycle-runner manifests do not hold their invariants"
note "cycle-runner CronJob holds its invariants (Forbid, deadlines, backoffLimit 0, Secret, PVC, no op, tini)"
note "gatekeeper listener holds its invariants (1 replica, Recreate, PVC, Secret, cpu request, Telegram files only)"
note "PO chat holds its invariants (1 replica, Recreate, PVC, Secret, CLAUDE_CONFIG_DIR, cpu request, memory limit, three Secret files)"
