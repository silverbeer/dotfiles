#!/usr/bin/env bash
# One-time: bootstrap the Linear personal API key used by the linear-crud
# skill's linear-gql.sh wrapper, sourced from 1Password. Non-fatal — if op is
# locked, it prints the manual command and `doctor.sh` will flag it.
set -uo pipefail

KEY_FILE="$HOME/.config/linear/gql-key"
OP_REF='op://agents/linear_api_key/password'

if [ -f "$KEY_FILE" ] && [ -s "$KEY_FILE" ]; then
  echo "linear gql-key already present — skipping"
  exit 0
fi

mkdir -p "$(dirname "$KEY_FILE")"
if command -v op >/dev/null 2>&1 && op read "$OP_REF" >"$KEY_FILE" 2>/dev/null && [ -s "$KEY_FILE" ]; then
  chmod 600 "$KEY_FILE"
  echo "linear gql-key created from 1Password ($KEY_FILE)"
else
  rm -f "$KEY_FILE"
  echo "WARN: could not create $KEY_FILE from 1Password (op locked or missing)."
  echo "  Run this once, then re-check with the doctor:"
  echo "    op read '$OP_REF' > $KEY_FILE && chmod 600 $KEY_FILE"
fi
exit 0
