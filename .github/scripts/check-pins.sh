#!/usr/bin/env bash
# The ruff version is pinned in two places that have to agree:
#
#   ci.yml       RUFF_VERSION      — what gets installed
#   .ruff.toml   required-version  — what ruff refuses to run as
#
# If they drift, ruff aborts on every PR with "required version does not match"
# and the failure looks like a broken lint rather than a stale pin. Worse, if
# someone "fixes" it by loosening required-version, the pin stops meaning
# anything and the rule set silently drifts between ruff releases.
#
# Needs no binaries, so this one always runs.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

wf="$REPO/.github/workflows/ci.yml"
cfg="$REPO/.ruff.toml"
[ -f "$wf" ] || die "missing $wf"
[ -f "$cfg" ] || die "missing $cfg"

ci_ver="$(sed -n 's/^[[:space:]]*RUFF_VERSION:[[:space:]]*"\{0,1\}\([^"[:space:]]*\)"\{0,1\}[[:space:]]*$/\1/p' "$wf")"
cfg_ver="$(sed -n 's/^required-version[[:space:]]*=[[:space:]]*"==\([^"]*\)".*$/\1/p' "$cfg")"

[ -n "$ci_ver" ] || die "could not read RUFF_VERSION from .github/workflows/ci.yml"
[ -n "$cfg_ver" ] || die "could not read an '==x.y.z' required-version from .ruff.toml"

if [ "$ci_ver" != "$cfg_ver" ]; then
  die "ruff pin drift: ci.yml RUFF_VERSION=$ci_ver but .ruff.toml required-version===$cfg_ver"
fi

note "ruff pinned consistently at $ci_ver"
