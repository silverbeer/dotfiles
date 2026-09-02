#!/usr/bin/env bash
# The other half of k3s/cycle-runner/claude-cli-contract.sh.
#
# That script asserts, at image build time, that every flag in its list exists
# in `claude --help`. It cannot notice the opposite mistake: someone adds a
# flag to a `claude -p` invocation in the runner and does not add it to the
# contract. The build stays green, the flag is unguarded, and the next `claude`
# release that renames it takes the runner down at 2am — which is the exact
# failure the contract exists to prevent.
#
# So this greps the runner for the flags it actually passes and fails if one is
# not in the contract. One place, checked from both directions.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

CONTRACT="$REPO/k3s/cycle-runner/claude-cli-contract.sh"
SCRIPTS="$REPO/dot_claude/skills/cycle-runner/scripts"

[ -r "$CONTRACT" ] || die "no contract script at $CONTRACT"

# The flags the contract promises to guard, read out of its REQUIRED_FLAGS
# array rather than duplicated here.
declared="$WORK/declared.txt"
sed -n '/^REQUIRED_FLAGS=(/,/^)/p' "$CONTRACT" \
  | grep -oE -- '--[a-zA-Z][a-zA-Z-]*' | sort -u >"$declared"
[ -s "$declared" ] || die "could not read REQUIRED_FLAGS out of $CONTRACT"

# The flags actually passed to `claude`. An invocation is a `claude ` token
# followed by continuation lines; take the flags from that line and every
# backslash-continued line after it. `-p` is the short form of --print, which
# is what `claude --help` prints, so map it.
#
# The leading boundary is asserted, the trailing one is NOT, deliberately.
# `grep -o` does not return overlapping matches, so a trailing [[:space:]] in
# the pattern eats the separator that the NEXT flag needs as its leading
# boundary: on `claude -p --resume "$id"` it matched `-p` and silently dropped
# `--resume`, leaving the contract's most important flag unchecked while the
# check reported success.
used="$WORK/used.txt"
: >"$used"
for f in "$SCRIPTS"/*.sh; do
  [ -e "$f" ] || continue
  awk '
    # Comments are prose. run.sh and triage-run.sh both discuss `claude -p` in
    # theirs, and a comment that mentions a flag is not a dependency on it.
    /^[[:space:]]*#/ { next }

    /(^|[^[:alnum:]_-])claude[[:space:]]+-/ { inv = 1 }

    inv {
      line = $0
      # Everything from the first `"/` to end of line is the PROMPT literal —
      # `-p "/triage --session-id X --run-id Y"`. Those are slash-command
      # options, parsed by the skill, not by `claude`; --run-id is not a CLI
      # flag at all. Reporting it as uncovered is a false failure, and a check
      # that cries wolf gets deleted.
      #
      # Pairing quotes instead does NOT work here: the invocation is wrapped in
      # `"$( ... )"`, so the opening quote belongs to the command substitution
      # and every pair after it straddles the wrong boundaries — measured, it
      # swallowed --session-id and kept --run-id, exactly backwards.
      sub(/"\/.*$/, " ", line)
      print line
    }

    inv && !/\\[[:space:]]*$/ { inv = 0 }
  ' "$f" | grep -oE -- '(^|[[:space:]])--?[a-zA-Z][a-zA-Z-]*' \
    | tr -d ' ' >>"$used" || true
done
sed -i.bak 's/^-p$/--print/' "$used" && rm -f "$used.bak"
sort -u -o "$used" "$used"
[ -s "$used" ] || die "found no claude invocations under $SCRIPTS — has the grep gone stale?"

missing="$(comm -23 "$used" "$declared" || true)"
if [ -n "$missing" ]; then
  err "these flags are passed to \`claude\` by the runner but are not in the contract:"
  printf '%s\n' "$missing" | sed 's/^/    /' >&2
  die "add them to REQUIRED_FLAGS in k3s/cycle-runner/claude-cli-contract.sh"
fi

note "claude CLI contract covers all $(wc -l <"$used" | tr -d ' ') flags the runner passes"
