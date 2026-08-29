#!/usr/bin/env bash
# .chezmoiignore ignores .claude/skills/* wholesale, so a new skill needs an
# explicit `!.claude/skills/<name>` exception or `chezmoi add` silently syncs
# nothing. Ask chezmoi what it ACTUALLY manages rather than grepping
# .chezmoiignore for the line: grep passes on a typo, on a source dir named
# something other than the exception, and on any other rule in the file that
# happens to re-ignore the path.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

managed="$WORK/managed.txt"
cm managed >"$managed"

rc=0

# Forward: every skill source dir must have managed FILES.
#
# Checking for the bare `.claude/skills/<name>` line is not enough. chezmoi
# keeps listing the directory entry even when a later rule re-ignores
# everything inside it (`.claude/skills/todo/**` does exactly that), so a skill
# can be "managed" and still sync nothing at all — which is the precise failure
# this check exists to catch. Count the children instead.
for d in "$REPO"/dot_claude/skills/*/; do
  [ -d "$d" ] || continue
  n="$(basename "$d")"
  files="$(grep -c "^\.claude/skills/$n/" "$managed" || true)"
  if [ "$files" -gt 0 ]; then
    note "ok   $n ($files managed file(s))"
  elif grep -qx ".claude/skills/$n" "$managed"; then
    err_file .chezmoiignore \
      "skill '$n' is listed but NONE of its files are managed — a later rule in .chezmoiignore is re-ignoring its contents"
    rc=1
  else
    err_file .chezmoiignore \
      "skill '$n' is not managed — add '!.claude/skills/$n' to .chezmoiignore"
    rc=1
  fi
done

# Reverse: every exception must have a source dir, so a rename or a deletion
# leaves a dangling line instead of quietly doing nothing.
while read -r n; do
  # A heredoc fed by an empty $(...) still yields one blank line; without this
  # guard the -d test below would resolve to the skills/ dir itself and pass.
  [ -n "$n" ] || continue
  if [ -d "$REPO/dot_claude/skills/$n" ]; then
    note "ok   !$n"
  else
    err_file .chezmoiignore \
      "exception '!.claude/skills/$n' has no dot_claude/skills/$n directory"
    rc=1
  fi
done <<EOF
$(sed -n 's|^!\.claude/skills/\([^/]*\)$|\1|p' "$REPO/.chezmoiignore")
EOF

exit "$rc"
