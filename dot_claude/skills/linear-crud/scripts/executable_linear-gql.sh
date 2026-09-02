#!/usr/bin/env bash
# linear-gql.sh — run a raw GraphQL query/mutation against the Linear API.
# The key is read internally and never printed. Scope: only ever talks to
# api.linear.app.
#
# Key sources, in order: $LINEAR_API_KEY, then the key file
# (~/.config/linear/gql-key, override: LINEAR_KEY_FILE). Nothing else — the
# CLI's credentials.toml is deliberately NOT scraped (SB-923). doctor.sh shows
# how to bootstrap the key file from op.
#
# Transient failures (curl error, HTTP 429, 5xx) are retried 3× with backoff
# (1s, 2s, 4s) — for QUERIES only. A mutation gets exactly one attempt: a
# retry after an ambiguous failure could apply the write twice. Any other 4xx
# (401 bad key, 400 bad query) fails immediately.
#
# Usage:
#   bash linear-gql.sh '{ viewer { id name email } }'
#   echo '<query>' | bash linear-gql.sh
#   bash linear-gql.sh "$(cat query.graphql)"
#   bash linear-gql.sh '<query>' '{"var":1}'          # variables as JSON in $2
set -euo pipefail

KEY_FILE="${LINEAR_KEY_FILE:-$HOME/.config/linear/gql-key}"
KEY="${LINEAR_API_KEY:-}"
if [ -z "$KEY" ] && [ -f "$KEY_FILE" ]; then
  KEY=$(cat "$KEY_FILE")
fi
# Strip whitespace from BOTH sources, not just the file.
#
# The file path always did; the environment path did not, and on the mini that
# never mattered because ~/.zshenv reads the key as `$(<file)`, which strips
# the trailing newline `op read > file` leaves. A k8s `secretKeyRef` does not
# strip anything, so the pod got a key ending in \n and sent
#
#     Authorization: lin_api_…\n
#
# The embedded newline terminated the header block early: curl's Content-Type
# never reached Linear, which saw the default x-www-form-urlencoded and
# rejected the request as possible CSRF. The symptom named neither the
# credential nor the newline — over HTTP/2 it was only "PROTOCOL_ERROR", and
# over HTTP/1.1 a 400 about content-type.
#
# A newline in a header value is a header-injection shape regardless of where
# the key came from, so this is defence, not a workaround for one provisioner.
KEY="$(printf '%s' "$KEY" | tr -d ' \t\r\n')"
[ -z "$KEY" ] && { echo "linear-gql: no API key — set LINEAR_API_KEY or create $KEY_FILE (run doctor.sh)" >&2; exit 1; }

QUERY="${1:-$(cat)}"
[ -z "$QUERY" ] && { echo "linear-gql: empty query" >&2; exit 1; }

# Optional GraphQL variables as a JSON string in $2 (or $LINEAR_VARS).
VARS="${2:-${LINEAR_VARS:-}}"
if [ -n "$VARS" ]; then
  PAYLOAD=$(jq -nc --arg q "$QUERY" --argjson v "$VARS" '{query:$q, variables:$v}')
else
  PAYLOAD=$(jq -nc --arg q "$QUERY" '{query:$q}')
fi

BODY="$(mktemp "${TMPDIR:-/tmp}/linear-gql.XXXXXX")"
trap 'rm -f "$BODY" "$BODY.err"' EXIT

# Mutations are never retried. Strip leading whitespace and #-comments before
# looking at the first keyword.
stripped="$(sed -e 's/^[[:space:]]*//' -e '/^#/d' -e '/^$/d' <<<"$QUERY" | head -1)"
case "$stripped" in
  mutation*) max_attempts=1 ;;
  *)         max_attempts=4 ;;
esac

delay=1
curl_err=""
for attempt in $(seq 1 "$max_attempts"); do
  # --http1.1 is NOT a preference. curl 7.88 (debian bookworm, which is what
  # the cycle-runner image is built on) cannot complete an authenticated POST
  # to api.linear.app over HTTP/2: every attempt dies with
  #
  #   curl: (92) HTTP/2 stream 1 was not closed cleanly: PROTOCOL_ERROR (err 1)
  #
  # Reproduced in-cluster, 3/3 retries, twice; the same request with --http1.1
  # gets a real response. Retrying does not help because it is not transient.
  #
  # An unauthenticated one-line query DOES succeed over h2, which is why this
  # was invisible until a pod ran a real query — worth knowing before anyone
  # "verifies" it with a trivial curl and concludes the flag is unnecessary.
  #
  # HTTP/2 buys nothing here: one small request, no multiplexing, no server
  # push. So this is forced everywhere rather than gated on the environment —
  # a flag that only applies in the pod is a difference nobody would remember.
  status="$(curl -sS --http1.1 https://api.linear.app/graphql \
    --connect-timeout 10 --max-time 60 \
    -H "Authorization: $KEY" \
    -H "Content-Type: application/json" \
    --data "$PAYLOAD" -o "$BODY" -w '%{http_code}' 2>"$BODY.err")" || status=000
  curl_err="$(cat "$BODY.err" 2>/dev/null || true)"
  case "$status" in
    2*) cat "$BODY"; exit 0 ;;
    000|429|5*) ;;                       # transient: fall through to retry
    *)  break ;;                         # other 4xx: retrying cannot help
  esac
  [ "$attempt" -lt "$max_attempts" ] || break
  echo "linear-gql: HTTP $status, retry $attempt/$((max_attempts - 1)) in ${delay}s" >&2
  sleep "$delay"; delay=$((delay * 2))
done

# Final failure: status + a body excerpt. The body is Linear's response, never
# the request, so it cannot contain the key.
{
  printf 'linear-gql: HTTP %s' "$status"
  [ "$status" = 000 ] && printf ' (curl failed: %s)' "${curl_err:-no stderr}"
  printf ' — '; head -c 200 "$BODY"; echo
} >&2
exit 1
