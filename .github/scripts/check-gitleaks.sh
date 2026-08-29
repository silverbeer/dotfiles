#!/usr/bin/env bash
# Scan history AND the working tree.
#
# --log-opts=--all because a diff-only scan cannot see a key that is already on
# main. --redact because this repo is public and a real finding must never put
# the secret itself into an Actions log.
#
# GITLEAKS_CONFIG lets .github/tests/ point the same scanner at a fixture repo,
# with either this repo's allowlists or a bare default config.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

command -v gitleaks >/dev/null 2>&1 || die "gitleaks is not installed"
: "${GITLEAKS_CONFIG:="$REPO/.gitleaks.toml"}"
[ -f "$GITLEAKS_CONFIG" ] || die "gitleaks config not found: $GITLEAKS_CONFIG"

rc=0
gitleaks version

# Only scan history if there is history to scan; the tests also use plain dirs.
if [ -d "$REPO/.git" ]; then
  gitleaks git "$REPO" --log-opts=--all --config "$GITLEAKS_CONFIG" --redact --no-banner || rc=1
else
  note "no .git — skipping the history scan"
fi

gitleaks dir "$REPO" --config "$GITLEAKS_CONFIG" --redact --no-banner || rc=1

exit "$rc"
