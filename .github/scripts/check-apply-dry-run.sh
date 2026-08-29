#!/usr/bin/env bash
# HARD RULE: every apply in this file is --dry-run. A bare `chezmoi apply`
# executes the run_* scripts, which call `op read` and `brew install`.
# check-no-bare-apply.sh enforces that the flag is still there.
#
# Two passes, because they cover different branches of modify_settings.json.tmpl:
#   1. empty HOME    -> the cur='{}' branch, and proves dry-run writes nothing
#   2. seeded HOME   -> the merge branch, and proves dry-run mutates nothing
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

command -v jq >/dev/null 2>&1 || die "jq is not installed"
rc=0

note "pass 1: apply --dry-run into an empty HOME"
rm -rf "$DEST"; mkdir -p "$DEST"
cm apply --dry-run -v >"$WORK/apply-empty.log" 2>&1 || {
  err "apply --dry-run failed against an empty HOME"
  sed -n '1,40p' "$WORK/apply-empty.log" >&2
  rc=1
}
assert_dir_empty "$DEST" "apply --dry-run wrote into the scratch HOME" || rc=1

note "pass 2: apply --dry-run over an existing settings.json"
rm -rf "$DEST"; mkdir -p "$DEST/.claude"
jq -n '{permissions:{allow:["Bash","Bash(gh api:*)"]},theme:"light-daltonized",tui:{diffMode:"inline"}}' \
  >"$DEST/.claude/settings.json"
cp "$DEST/.claude/settings.json" "$WORK/settings.before.json"
cm apply --dry-run -v >"$WORK/apply-seeded.log" 2>&1 || {
  err "apply --dry-run failed against a seeded HOME"
  sed -n '1,40p' "$WORK/apply-seeded.log" >&2
  rc=1
}
assert_unchanged "$WORK/settings.before.json" "$DEST/.claude/settings.json" \
  "apply --dry-run modified the seeded settings.json" || rc=1

# Nothing beyond the file we seeded ourselves may exist.
find "$DEST" -mindepth 1 ! -path "$DEST/.claude" ! -path "$DEST/.claude/settings.json" \
  >"$WORK/extra.txt"
if [ -s "$WORK/extra.txt" ]; then
  err "apply --dry-run created paths alongside the seeded settings.json"
  sed "s|^$DEST|<dest>|" "$WORK/extra.txt" >&2
  rc=1
fi

exit "$rc"
