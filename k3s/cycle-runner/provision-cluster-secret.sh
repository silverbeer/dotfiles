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

# Every value is TRIMMED of trailing newlines before it goes in.
#
# This is not tidiness. `~/.config/linear/gql-key` is written by
# `op read ... > file`, so it ends with a newline; on the mini nothing noticed,
# because ~/.zshenv reads it as `$(<file)` and command substitution strips
# trailing newlines. `secretKeyRef` does not. The pod therefore got
#
#     Authorization: lin_api_…\n
#
# and the embedded newline broke the rest of the header block — curl's
# Content-Type never reached Linear, which saw the default
# application/x-www-form-urlencoded and rejected the call as possible CSRF.
# Two layers of misleading symptom: over HTTP/2 it surfaced only as
# "PROTOCOL_ERROR", and over HTTP/1.1 as a 400 about content-type, neither of
# which points at a stray byte in a credential.
#
# `gh auth token` prints a newline too, so GH_TOKEN had the same defect.
trimmed="$(mktemp -d)"
trap 'rm -rf "$trimmed"' EXIT
chmod 700 "$trimmed"

put() {  # KEY SOURCE-FILE
  local key="$1" src="$2" dst="$trimmed/$1"
  # printf '%s' "$(cat …)" — command substitution strips the trailing
  # newline(s) and printf adds none back.
  printf '%s' "$(cat "$src")" >"$dst"
  chmod 600 "$dst"
  [ -s "$dst" ] || die "$src is empty after trimming"
  args+=(--from-file="$key=$dst")
  local n_src n_dst
  n_src="$(wc -c <"$src" | tr -d ' ')"
  n_dst="$(wc -c <"$dst" | tr -d ' ')"
  if [ "$n_src" != "$n_dst" ]; then
    echo "  ok    $key  ($n_dst bytes, trimmed from $n_src)"
  else
    echo "  ok    $key  ($n_dst bytes)"
  fi
}

args=()
while IFS=$'\t' read -r key path; do
  [ -z "$key" ] && continue
  [ -r "$path" ] || die "missing $path — run: bash ~/.claude/skills/cycle-runner/scripts/provision-secrets.sh"
  [ -s "$path" ] || die "$path is empty"
  put "$key" "$path"
done <<<"$FILES"

# The GitHub token is the one credential with no file on the mini: `gh` keeps
# it in the login keychain, which a pod cannot reach. `gh auth token` prints
# it, so it goes in via a process substitution rather than a temp file or an
# --from-literal that would land in this shell's history.
command -v gh >/dev/null 2>&1 || die "gh is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not logged in — run: gh auth login"
gh_token_file="$trimmed/.gh-raw"
gh auth token >"$gh_token_file"
chmod 600 "$gh_token_file"
[ -s "$gh_token_file" ] || die "gh auth token produced nothing"
put gh-token "$gh_token_file"

# --dry-run=client | apply, rather than `delete` then `create`: a failure
# partway through the latter leaves the runner with no credentials at all.
kubectl create secret generic "$NAME" \
  --namespace "$NS" \
  "${args[@]}" \
  --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null

echo
echo "Secret $NS/$NAME:"
# Byte counts and a trailing-whitespace assertion, never a value. The assertion
# is the point: a trailing newline here is invisible, survives into an env var,
# and breaks an HTTP header block far away from anything that looks like a
# credential problem.
kubectl -n "$NS" get secret "$NAME" -o jsonpath='{.data}' \
  | jq -r 'to_entries[]
           | (.value | @base64d) as $v
           | "  \(.key)  (\($v | length) bytes)\(if ($v | test("\\s$")) then "  ** ENDS IN WHITESPACE **" else "" end)"'

if kubectl -n "$NS" get secret "$NAME" -o jsonpath='{.data}' \
   | jq -e 'to_entries | map(.value | @base64d | test("\\s$")) | any' >/dev/null; then
  die "a Secret value ends in whitespace — it will corrupt an HTTP header or an env var"
fi
