#!/usr/bin/env bash
# Create (or rotate) the `cycle-runner` Secret in the cluster.
#
#   bash k3s/cycle-runner/provision-cluster-secret.sh
#
# Reads from the files the mini already has, NOT from 1Password. `op` is not
# called here and must not be: SB-974 established that `op read` prefers the
# desktop app once a desktop account exists and raises a Touch ID prompt that
# has wedged for twelve minutes. The three cycle-runner secrets are already on
# disk because provision-secrets.sh put them there from a real terminal; this
# just copies them into the cluster.
#
# Re-run it whenever a secret rotates. It replaces the Secret in place; the
# next tick picks it up, since a CronJob reads the Secret at pod start.
#
# Never prints a value. Success is judged by exit code and byte count.
set -euo pipefail

NS=cycle-runner
NAME=cycle-runner
CR_SECRETS_DIR="${CYCLE_RUNNER_SECRETS_DIR:-$HOME/.config/cycle-runner}"
LINEAR_KEY_FILE="${LINEAR_KEY_FILE:-$HOME/.config/linear/gql-key}"

die() { echo "provision-cluster-secret: $*" >&2; exit 1; }

command -v kubectl >/dev/null 2>&1 || die "kubectl is not installed"
kubectl get ns "$NS" >/dev/null 2>&1 \
  || die "namespace $NS does not exist — kubectl apply -f k3s/cycle-runner/namespace.yaml"

# key <TAB> source file
FILES=$(
  cat <<EOF2
claude-token	$CR_SECRETS_DIR/claude-token
telegram-token	$CR_SECRETS_DIR/telegram-token
telegram-chat-id	$CR_SECRETS_DIR/telegram-chat-id
linear-api-key	$LINEAR_KEY_FILE
EOF2
)

args=()
while IFS=$'\t' read -r key path; do
  [ -z "$key" ] && continue
  [ -r "$path" ] || die "missing $path — run: bash ~/.claude/skills/cycle-runner/scripts/provision-secrets.sh"
  [ -s "$path" ] || die "$path is empty"
  args+=(--from-file="$key=$path")
  echo "  ok    $key  ($(wc -c <"$path" | tr -d ' ') bytes)"
done <<<"$FILES"

# The GitHub token is the one credential with no file on the mini: `gh` keeps
# it in the login keychain, which a pod cannot reach. `gh auth token` prints
# it, so it goes in via a process substitution rather than a temp file or an
# --from-literal that would land in this shell's history.
command -v gh >/dev/null 2>&1 || die "gh is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not logged in — run: gh auth login"
gh_token_file="$(mktemp)"
trap 'rm -f "$gh_token_file"' EXIT
gh auth token >"$gh_token_file"
[ -s "$gh_token_file" ] || die "gh auth token produced nothing"
args+=(--from-file="gh-token=$gh_token_file")
echo "  ok    gh-token  ($(wc -c <"$gh_token_file" | tr -d ' ') bytes)"

# --dry-run=client | apply, rather than `delete` then `create`: a failure
# partway through the latter leaves the runner with no credentials at all.
kubectl create secret generic "$NAME" \
  --namespace "$NS" \
  "${args[@]}" \
  --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null

echo
echo "Secret $NS/$NAME has $(kubectl -n "$NS" get secret "$NAME" -o jsonpath='{.data}' | jq 'keys | length') keys:"
kubectl -n "$NS" get secret "$NAME" -o jsonpath='{.data}' | jq -r 'to_entries[] | "  \(.key)  (\(.value | @base64d | length) bytes)"'
