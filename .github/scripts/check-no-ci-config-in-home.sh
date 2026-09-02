#!/usr/bin/env bash
# Root-level source entries that are NOT dot-prefixed become chezmoi targets.
# That is how ~/README.md got written for months, and it is what would happen
# the moment someone "tidied" .ruff.toml into ruff.toml: CI config, and the
# tests directory, would start deploying into every machine's $HOME.
#
# The .chezmoiignore entries for these names are NOT a second line of defence.
# They list the DOTTED names, so a rename to `ruff.toml` sails straight past
# them. chezmoi skipping dot-prefixed source entries is the only thing holding,
# and this check is what notices when that stops being true.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

managed="$WORK/managed.txt"
cm managed >"$managed"

# Anything on this list appearing as a chezmoi target is a bug.
# k3s/ holds cluster manifests and the cycle-runner Dockerfile (SB-975). Like
# README.md it is not dot-prefixed, so ONLY its .chezmoiignore entry keeps it
# out of $HOME — delete that line and every machine grows a ~/k3s at the next
# apply, which is precisely how ~/README.md happened.
leaks='^(\.?ruff\.toml|\.?gitleaks\.toml|\.?github(/|$)|README\.md|SETUP\.md|tests(/|$)|k3s(/|$))'

if grep -nE "$leaks" "$managed"; then
  die "repo-only files are being deployed into \$HOME — keep them dot-prefixed and listed in .chezmoiignore"
fi

note "no CI config, docs or tests in the managed set ($(wc -l <"$managed" | tr -d ' ') targets)"
