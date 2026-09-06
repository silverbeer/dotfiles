#!/usr/bin/env bash
# Run /code-review over a worktree and print its findings as JSON (SB-983).
#
#   code-review.sh <worktree> [level]     -> findings array on stdout
#
# Exit 0 = the review ran and returned a parseable verdict (possibly `[]`).
# Exit 3 = the review did not return a verdict. NOT a pass — the caller must
#          treat it as blocked. Silence is inconclusive; that rule stands.
#
# ------------------------------------------------------------------ why
#
# `/work-headless` phase 6 used to say "run /code-review high" and then look
# for findings text in its own turn. Invoked that way the agent calls the
# Skill tool, which answers "Skill execution completed" — a completion marker,
# not a review. The findings had gone somewhere the calling turn did not read.
# Phase 6 saw no text, correctly refused to call silence a pass, and opened a
# blocked gate. That happened on SB-870 and would have happened on every run.
#
# Measured 2026-09-05, running `claude -p "/code-review high"` as its own
# process, which is what this script does:
#
#   a diff with real bugs -> a fenced ```json array of finding objects
#   a genuinely clean diff -> prose, then a fenced ```json `[]`
#
# Neither is empty. The verdict is machine-readable in both cases, so phase 6
# stops depending on what a model chose to narrate and starts reading a
# contract.
set -uo pipefail

WORKTREE="${1:-}"
LEVEL="${2:-high}"
[ -n "$WORKTREE" ] || { echo "usage: code-review.sh <worktree> [level]" >&2; exit 2; }
[ -d "$WORKTREE" ] || { echo "code-review: no such worktree: $WORKTREE" >&2; exit 2; }

command -v claude >/dev/null 2>&1 || { echo "code-review: claude not on PATH" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || { echo "code-review: jq not on PATH" >&2; exit 2; }

# Its own process, its own session. A fresh `-p` turn makes the slash command
# the whole prompt, so the review's answer IS the result body.
#
# Read-only tools: a reviewer that can edit is not a reviewer. This is also
# what stops it "fixing" what it found and reporting clean.
out="$(cd "$WORKTREE" && claude -p "/code-review $LEVEL" \
        --output-format json \
        --permission-mode acceptEdits \
        --allowedTools "Bash,Read,Grep,Glob" 2>&1)" || {
  echo "code-review: claude exited non-zero" >&2
  printf '%s\n' "$out" | tail -5 >&2
  exit 3
}

body="$(jq -r '.result // ""' <<<"$out" 2>/dev/null)"
if [ -z "$body" ]; then
  echo "code-review: the review returned an empty body — inconclusive, not clean" >&2
  exit 3
fi

# The last fenced json block is the verdict. Last, not first: the review may
# quote a snippet earlier in its prose, and the verdict is what it ends on.
findings="$(printf '%s\n' "$body" \
  | awk '/^```json$/{buf=""; inb=1; next} /^```$/{if(inb){last=buf; inb=0}; next} inb{buf=buf $0 "\n"} END{printf "%s", last}')"

if [ -z "$findings" ]; then
  echo "code-review: no json verdict in the review body — inconclusive, not clean" >&2
  printf '%s\n' "$body" | head -20 >&2
  exit 3
fi

if ! jq -e 'type == "array"' >/dev/null 2>&1 <<<"$findings"; then
  echo "code-review: the verdict is not a json array — inconclusive, not clean" >&2
  exit 3
fi

jq -c '.' <<<"$findings"
