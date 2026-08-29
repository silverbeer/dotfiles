#!/usr/bin/env bash
# Run shellcheck over the shell in this repo.
#
#   no args : the full tracked *.sh set, plus the two shell scripts the *.sh
#             glob misses (see below).
#   args    : exactly those files instead. ci.yml's chezmoi job uses this to
#             check the RENDERED modify_settings script.
#
# Two behaviours, both exercised by .github/tests/test_shellcheck.sh — a check
# that quietly does nothing when handed the wrong input is the failure mode
# this whole test suite exists to prevent.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

command -v shellcheck >/dev/null 2>&1 || die "shellcheck is not installed"
cd "$REPO"

if [ "$#" -gt 0 ]; then
  note "checking $# explicitly named file(s)"
  shellcheck --shell=bash "$@"
  exit 0
fi

files=()
while IFS= read -r f; do files+=("$f"); done < <(git ls-files '*.sh')
[ "${#files[@]}" -gt 0 ] || die "git ls-files '*.sh' matched nothing — wrong REPO?"
note "checking ${#files[@]} tracked *.sh"
shellcheck "${files[@]}"

# Named .json.tmpl, but it is a bash script — chezmoi runs it to produce
# ~/.claude/settings.json.
shellcheck dot_claude/modify_settings.json.tmpl

# De-template the brew installer before checking it. DELETE the whole-line
# {{ ... }} directives; blanking them yields a spurious SC1128 (shebang no
# longer on line 1).
sed -E '/^\{\{.*\}\}[[:space:]]*$/d' run_onchange_install-brew-tools.sh.tmpl | shellcheck -
note "checked the 2 shell scripts the *.sh glob misses"
