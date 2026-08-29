#!/usr/bin/env bash
# ruff over the tracked *.py.
#
#   no args : the full tracked *.py set.
#   args    : exactly those files instead (used by the tests to prove ruff is
#             actually enforcing the selected rules, not just exiting 0).
#
# RUFF_CONFIG overrides the config; the tests use it to prove required-version
# is enforced without needing a second ruff on the machine.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

command -v ruff >/dev/null 2>&1 || die "ruff is not installed"
: "${RUFF_CONFIG:="$REPO/.ruff.toml"}"
[ -f "$RUFF_CONFIG" ] || die "ruff config not found: $RUFF_CONFIG"
cd "$REPO"

# --no-cache: never leave a .ruff_cache behind in a checkout under test.
if [ "$#" -gt 0 ]; then
  note "checking $# explicitly named file(s)"
  ruff check --config "$RUFF_CONFIG" --no-cache "$@"
  exit 0
fi

files=()
while IFS= read -r f; do files+=("$f"); done < <(git ls-files '*.py')
[ "${#files[@]}" -gt 0 ] || die "git ls-files '*.py' matched nothing — wrong REPO?"
note "checking ${#files[@]} tracked *.py"
ruff check --config "$RUFF_CONFIG" --no-cache "${files[@]}"
