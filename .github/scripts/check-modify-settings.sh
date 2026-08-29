#!/usr/bin/env bash
# Exercise dot_claude/modify_settings.json.tmpl the way chezmoi does: render the
# template, then feed it the CURRENT settings.json on stdin and inspect what it
# emits. The contract is:
#
#   ENFORCE  hooks + statusLine    always canonical, live value discarded
#   UNION    permissions.allow     base union machine-added, never removes
#   SEED     plugins / theme       default only when absent, live wins
#   PRESERVE everything else       tui, warnings, keys that do not exist yet
#
# and the whole thing must be idempotent, or every `chezmoi apply` shows a diff.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

command -v jq >/dev/null 2>&1 || die "jq is not installed"

tmpl=dot_claude/modify_settings.json.tmpl
[ -f "$REPO/$tmpl" ] || die "missing $REPO/$tmpl"
home="$(cm execute-template '{{ .chezmoi.homeDir }}')"

# execute-template reads its template from stdin when given no file args, so
# the script has to be fed through process substitution to keep stdin free for
# the settings.json it merges.
cd "$REPO"
run() { printf '%s' "$1" | bash <(cm execute-template -f "$tmpl"); }

# A realistic live settings.json: Claude Code owns theme/tui, and the machine
# has added a permission that must survive the union.
seed="$(jq -n '{
  permissions: { allow: ["Bash", "Bash(gh api:*)"] },
  theme: "light-daltonized",
  tui: { diffMode: "inline" },
  skipWorkflowUsageWarning: true,
  hooks: { PreToolUse: [] },
  statusLine: { type: "command", command: "/stale" }
}')"

out1="$(run "$seed")"
printf '%s' "$out1" | jq -e . >/dev/null || die "output is not valid JSON"

rc=0
check() {
  got="$(printf '%s' "$out1" | jq -r "$1")"
  if [ "$got" = "$2" ]; then
    note "ok   $1"
  else
    err "$1 == '$got', expected '$2'"
    rc=1
  fi
}
check '.theme' 'light-daltonized'                                  # live wins over the seeded default
check '.tui.diffMode' 'inline'                                     # unknown runtime keys preserved
check '.skipWorkflowUsageWarning' 'true'
check '.permissions.allow | index("Bash") != null' 'true'          # base permission still there
check '.permissions.allow | index("Bash(gh api:*)") != null' 'true' # machine-added permission survives
check '.permissions.allow | index("Bash(linear:*)") != null' 'true' # canonical permission added
check '.enabledPlugins["caveman@caveman"]' 'true'                  # seeded default appears
check '.hooks.PreToolUse[0].hooks[0].command' "$home/.claude/hooks/rtk-rewrite.sh"
check '.statusLine.command' "$home/.claude/statusLine.sh"          # enforced, not merged

out2="$(run "$out1")"
if [ "$out1" != "$out2" ]; then
  err "modify_settings.json.tmpl is not idempotent — every apply would show a diff"
  diff <(printf '%s\n' "$out1") <(printf '%s\n' "$out2") >&2 || true
  rc=1
fi

# The empty-stdin branch: a machine with no settings.json yet.
empty="$(run '')"
if ! printf '%s' "$empty" | jq -e '.theme == "dark-daltonized"' >/dev/null; then
  err "empty stdin did not produce the seeded defaults"
  rc=1
fi

if [ "$rc" -eq 0 ]; then
  note "valid JSON, runtime keys preserved, allow-list unioned, idempotent"
fi
exit "$rc"
