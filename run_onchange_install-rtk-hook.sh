#!/bin/bash
set -euo pipefail

# The RTK PreToolUse hook script (~/.claude/hooks/rtk-rewrite.sh) is a generated
# artifact, not tracked in this repo. settings.json.tmpl references it, so on a
# fresh machine we must regenerate it or the hook silently no-ops every Bash call.

if ! command -v rtk &> /dev/null; then
  echo "rtk not found, skipping hook install"
  exit 0
fi

# --hook-only: install the rewrite hook without touching CLAUDE.md/RTK.md.
# --no-patch: don't modify settings.json (chezmoi owns it via settings.json.tmpl).
rtk init -g --hook-only --no-patch
