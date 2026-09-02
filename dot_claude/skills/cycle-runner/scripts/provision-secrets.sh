#!/usr/bin/env bash
# Write the cycle-runner's secrets to local files, once, from a human terminal.
#
#   bash ~/.claude/skills/cycle-runner/scripts/provision-secrets.sh
#
# This is the ONLY script in the runner that talks to 1Password, and it is
# never on a tick's path (SB-974). Everything at runtime reads the files this
# writes — see cycle-runner/scripts/env.sh for why.
#
# Run it in a real terminal. `op` may raise the desktop-app prompt, which is
# fine here: a human is present, it happens once, and answering it is the point.
# On an unattended machine that same prompt wedges `op` indefinitely and stops
# the runner (SB-868), which is what this script exists to make impossible.
#
# Re-run it whenever a secret rotates. It is idempotent and overwrites.
set -euo pipefail

DEST="${CYCLE_RUNNER_SECRETS_DIR:-$HOME/.config/cycle-runner}"

# file  <TAB>  op reference
ITEMS=$(
  cat <<'EOF'
claude-token	op://agents/cycle-runner-claude/token
telegram-token	op://agents/cycle-runner-telegram/token
telegram-chat-id	op://agents/cycle-runner-telegram/chat-id
EOF
)

command -v op >/dev/null 2>&1 || {
  echo "op is not installed — brew install --cask 1password-cli" >&2
  exit 1
}

mkdir -p "$DEST"
chmod 700 "$DEST"

echo "Writing cycle-runner secrets to $DEST"
echo "(1Password may prompt — that is expected here, and only here.)"
echo

rc=0
while IFS=$'\t' read -r name ref; do
  [ -z "$name" ] && continue
  tmp="$DEST/.$name.tmp"
  # Never echo a value. Success is judged by exit code and byte count only.
  if op read "$ref" >"$tmp" 2>/dev/null && [ -s "$tmp" ]; then
    # Strip the trailing newline `op read` adds, so a consumer that does not
    # trim gets exactly the secret.
    printf '%s' "$(cat "$tmp")" >"$DEST/$name"
    chmod 600 "$DEST/$name"
    rm -f "$tmp"
    echo "  ok    $name  ($(wc -c <"$DEST/$name" | tr -d ' ') bytes)"
  else
    rm -f "$tmp"
    echo "  FAIL  $name  <- $ref" >&2
    rc=1
  fi
done <<<"$ITEMS"

echo
if [ "$rc" -eq 0 ]; then
  echo "Done. The runner no longer calls op at all — verify with:"
  echo "  bash ~/.claude/skills/linear-crud/scripts/doctor.sh"
else
  echo "Some secrets were not written. Check the vault reference and that this" >&2
  echo "terminal can read the 'agents' vault, then re-run." >&2
fi
exit "$rc"
