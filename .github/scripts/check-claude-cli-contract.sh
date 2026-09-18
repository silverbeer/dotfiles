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
# So this greps the runner, and the PO chat, for the flags they actually pass
# and fails if one is not in the contract. One place, checked from both
# directions.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

CONTRACT="$REPO/k3s/cycle-runner/claude-cli-contract.sh"
SCRIPTS="$REPO/dot_claude/skills/cycle-runner/scripts"
PO_CHAT="$REPO/dot_claude/skills/po-agent/scripts/po_chat.py"

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
[ -s "$used" ] || die "found no claude invocations under $SCRIPTS — has the grep gone stale?"
sed -i.bak 's/^-p$/--print/' "$used" && rm -f "$used.bak"
sort -u -o "$used" "$used"

# The PO chat (SB-1089) builds its argv as a python list, one quoted string per
# element, between two marker comments in claude_argv(). The markers are what
# this reads: a grep over the whole file would also pick up cycle_apply's
# --changes and --confirm, which are not claude flags.
#
# Kept as its OWN set, with its own count. Merging the two would mean a flag
# dropped from the runner still looked covered because the chat happened to
# pass it, which is exactly the drift this check exists to catch.
[ -r "$PO_CHAT" ] || die "no PO chat at $PO_CHAT — moved? update this check"
po_used="$WORK/po-used.txt"
po_flags="$(sed -n '/# claude-cli-contract: begin/,/# claude-cli-contract: end/p' "$PO_CHAT")"
[ -n "$po_flags" ] || die "found no '# claude-cli-contract: begin/end' markers in $PO_CHAT — has the grep gone stale?"
printf '%s\n' "$po_flags" | grep -oE -- '"--?[a-zA-Z][a-zA-Z-]*"' | tr -d '"' >"$po_used" \
  || die "found no claude flags between the markers in $PO_CHAT — has the grep gone stale?"
sed -i.bak 's/^-p$/--print/' "$po_used" && rm -f "$po_used.bak"
sort -u -o "$po_used" "$po_used"
[ -s "$po_used" ] || die "found no claude flags between the markers in $PO_CHAT — has the grep gone stale?"

# ...and nothing may pass a flag from OUTSIDE a marked block. A `claude` flag
# added elsewhere in the file would be invisible to the block above, unguarded
# by the contract, and silently broken by the next release that renames it.
# Flags for our own scripts (cycle_state.py, cycle_apply.py, argparse) live in
# `not-claude-argv` blocks, which say so.
stray="$(awk '
  /# claude-cli-contract: (begin|end)/ { claude = /begin/; next }
  /# not-claude-argv: (begin|end)/     { ours   = /begin/; next }
  claude || ours { next }
  /^[[:space:]]*#/ { next }
  { print FILENAME ":" FNR ": " $0 }
' "$PO_CHAT" | grep -E '[^[:alnum:]_][fFrRbBuU]{0,2}("|'"'"')--[a-zA-Z]' || true)"
if [ -n "$stray" ]; then
  err "these flag literals in $PO_CHAT are outside every marker block:"
  printf '%s\n' "$stray" | sed 's/^/    /' >&2
  die "put a claude flag between the '# claude-cli-contract' markers, or our own scripts' flags between '# not-claude-argv' markers"
fi

missing="$(comm -23 <(sort -u "$used" "$po_used") "$declared" || true)"
if [ -n "$missing" ]; then
  err "these flags are passed to \`claude\` by the runner or the PO chat but are not in the contract:"
  printf '%s\n' "$missing" | sed 's/^/    /' >&2
  die "add them to REQUIRED_FLAGS in k3s/cycle-runner/claude-cli-contract.sh"
fi

note "claude CLI contract covers all $(wc -l <"$used" | tr -d ' ') flags the runner passes"
note "claude CLI contract covers all $(wc -l <"$po_used" | tr -d ' ') flags the PO chat passes"
