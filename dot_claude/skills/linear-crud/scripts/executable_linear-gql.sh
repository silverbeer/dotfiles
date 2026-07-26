#!/usr/bin/env bash
# linear-gql.sh — run a raw GraphQL query/mutation against the Linear API,
# authenticating with the key the `linear` CLI already stored. The key is read
# internally and never printed. Scope: only ever talks to api.linear.app.
#
# Usage:
#   bash linear-gql.sh '{ viewer { id name email } }'
#   echo '<query>' | bash linear-gql.sh
#   bash linear-gql.sh "$(cat query.graphql)"        # variables not supported here
set -euo pipefail

CRED="${LINEAR_CRED:-$HOME/.config/linear/credentials.toml}"

# --fmt: print the credential file's structure with values REDACTED (field name
# + value length only), to debug key parsing without exposing the secret.
if [ "${1:-}" = "--fmt" ]; then
  [ -f "$CRED" ] || { echo "no cred file at $CRED" >&2; exit 1; }
  while IFS= read -r line; do
    case "$line" in
      *=*) name="${line%%=*}"; val="${line#*=}"; val="${val//\"/}"; val="${val# }"
           printf '%s= <%d chars, starts %s>\n' "$name" "${#val}" "$(printf '%s' "$val" | cut -c1-5)" ;;
      *)   printf '%s\n' "$line" ;;
    esac
  done < "$CRED"
  exit 0
fi

# Key resolution order: explicit env → dedicated key file → CLI credential toml.
KEY_FILE="${LINEAR_KEY_FILE:-$HOME/.config/linear/gql-key}"
KEY="${LINEAR_API_KEY:-}"
if [ -z "$KEY" ] && [ -f "$KEY_FILE" ]; then
  KEY=$(tr -d ' \t\r\n' < "$KEY_FILE")
fi
if [ -z "$KEY" ] && [ -f "$CRED" ]; then
  KEY=$(grep -oE 'lin_(api|oauth)_[A-Za-z0-9]+' "$CRED" 2>/dev/null | head -1 || true)
  if [ -z "$KEY" ]; then
    # Fallback: first long quoted or =-assigned token in the file.
    KEY=$(grep -oE '(=[[:space:]]*"?)[A-Za-z0-9_.-]{24,}' "$CRED" 2>/dev/null \
      | sed -E 's/^=[[:space:]]*"?//' | head -1 || true)
  fi
fi
[ -z "$KEY" ] && { echo "linear-gql: no API key found (set LINEAR_API_KEY or check $CRED)" >&2; exit 1; }

QUERY="${1:-$(cat)}"
[ -z "$QUERY" ] && { echo "linear-gql: empty query" >&2; exit 1; }

# Optional GraphQL variables as a JSON string in $2 (or $LINEAR_VARS).
VARS="${2:-${LINEAR_VARS:-}}"
if [ -n "$VARS" ]; then
  PAYLOAD=$(jq -nc --arg q "$QUERY" --argjson v "$VARS" '{query:$q, variables:$v}')
else
  PAYLOAD=$(jq -nc --arg q "$QUERY" '{query:$q}')
fi

curl -sS https://api.linear.app/graphql \
  -H "Authorization: $KEY" \
  -H "Content-Type: application/json" \
  --data "$PAYLOAD"
