#!/usr/bin/env bash
# Offline unit tests for the gatekeeper skill (SB-508): tg.py's Telegram
# transport (urlopen replaced, never a real socket — chunking, 409 raised by
# name and never retried, no secret in the message body) and gate.py's
# dual-channel gate logic (Telegram callback / free text, Linear comment
# parsing, first-decision-wins, timeout -> needs-human) against a fake `gql`
# (tests/fakes.py) — no subprocess, no Linear key, no network.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

tests_dir="$REPO/dot_claude/skills/gatekeeper/tests"
[ -d "$tests_dir" ] || die "missing $tests_dir — wrong REPO?"
[ -f "$REPO/dot_claude/skills/gatekeeper/scripts/gate.py" ] || die "missing gate.py — wrong REPO?"

# Never leave __pycache__ behind in the source tree — same reasoning as
# check-linear-crud.sh.
export PYTHONDONTWRITEBYTECODE=1

if out="$(cd "$REPO" && python3 -m unittest discover -s "$tests_dir" -p 'test_*.py' -v 2>&1)"; then
  note "$(printf '%s\n' "$out" | grep -E '^(Ran|OK)' || echo 'ran (summary line not found)')"
else
  err "gatekeeper unit tests failed:"
  printf '%s\n' "$out" | sed 's/^/    | /' >&2
  exit 1
fi

ran="$(printf '%s\n' "$out" | sed -nE 's/^Ran ([0-9]+) tests?.*/\1/p')"
# The floor tracks the suite. 25 was set when there were ~30 tests; at 70 it
# had stopped meaning anything — deleting the whole of test_gate.py (42 tests)
# still left 28 and passed, so the guard's own negative test broke before the
# guard did. Keep it just under the smaller file's count, so losing EITHER
# file is caught.
[ "${ran:-0}" -ge 60 ] || die "expected at least 60 tests to run, unittest reported '${ran:-none}' — discovery broken?"

note "check-gatekeeper: all offline tests passed"
