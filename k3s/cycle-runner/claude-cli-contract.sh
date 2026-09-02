#!/usr/bin/env bash
# The contract between the cycle-runner and the `claude` CLI, asserted at
# BUILD time so a removed or renamed flag fails a build instead of a 2am tick.
#
# This is the whole reason the Dockerfile pins CLAUDE_VERSION. Pinning alone
# only defers the problem to the day someone bumps it; this is what makes the
# bump fail loudly (SB-978 schedules exactly that bump, weekly).
#
# The list below is the set of flags run.sh and triage-run.sh actually pass —
# no more. Asserting a flag the runner does not use would block a build over a
# dependency that does not exist. .github/scripts/check-claude-cli-contract.sh
# keeps the two in step from the other direction: it greps the runner for the
# flags it passes and fails if one is missing from here.
#
# Deliberately NOT listed: --max-turns. `claude --help` on 2.1.251 does not
# have it; it was assumed once and the run failed. doctor.sh carries the same
# note. Nor --max-budget-usd or --no-session-persistence: both exist, neither
# is passed today, and a contract should assert what breaks us, not what might.
set -uo pipefail

REQUIRED_FLAGS=(
  --allowedTools
  --disallowedTools
  --json-schema
  --output-format
  --permission-mode
  --print
  --resume
  --session-id
)

rc=0
fail() { printf 'claude-cli-contract: %s\n' "$*" >&2; rc=1; }

command -v claude >/dev/null 2>&1 || { fail "claude is not on PATH"; exit 1; }

help="$(claude --help 2>&1)" || { fail "claude --help exited non-zero"; exit 1; }

for f in "${REQUIRED_FLAGS[@]}"; do
  # Word-boundary match: a bare grep for --allowed-tools also matches
  # --allowed-tools-something, and --print would match --print-anything.
  grep -qE -- "(^|[^a-zA-Z-])${f}([^a-zA-Z-]|$)" <<<"$help" \
    || fail "missing flag: $f"
done

# The absence that matters as much as the presences. SB-974: with no `op`
# binary the 1Password desktop path is not fixed, it is unreachable — no
# daemon to prefer, no Touch ID prompt to hang on (SB-868, SB-953, SB-972).
# An image that grew one, however innocently, silently re-opens that class.
if command -v op >/dev/null 2>&1; then
  fail "op is on PATH — the runner must never be able to reach 1Password at tick time (SB-974)"
fi

# gitleaks is load-bearing, not optional: SB-943 made the run-log secret scan
# fail closed, so without it the runner can never post a summary at all.
for b in git gh node python3 jq gitleaks; do
  command -v "$b" >/dev/null 2>&1 || fail "$b is not on PATH"
done

if [ "$rc" -eq 0 ]; then
  echo "claude-cli-contract: ok — ${#REQUIRED_FLAGS[@]} flags, $(claude --version | awk '{print $1}')"
fi
exit "$rc"
