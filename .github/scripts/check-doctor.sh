#!/usr/bin/env bash
# Offline tests for doctor.sh's cycle-runner + chezmoi-sync checks (SB-930):
# the hostname-gated claude-p / plist-deployment checks, gh / Telegram /
# stale-lock / chezmoi-status logic added to
# dot_claude/skills/linear-crud/scripts/executable_doctor.sh, run for real
# against a scratch $HOME with `op`, `gh`, `chezmoi`, `claude` and `launchctl`
# stubbed — never a real 1Password read, `gh` call, `claude -p`, Telegram
# request, or launchd query.
#
# doctor.sh is NEVER given a source guard the way run.sh has one (it is a
# script a human runs directly, `bash doctor.sh`, and always has been) — so
# every scenario below runs it end to end. The earlier sections (binaries,
# Linear API, skills+repos) are left to succeed or fail on whatever the host
# actually has; this file only asserts the lines the NEW checks print, not
# doctor.sh's overall exit status, which is unrelated to this ticket.
#
# `op` is stubbed to always fail in EVERY scenario, even ones that never
# touch it: cycle-runner/env.sh and gatekeeper/env.sh both silently `op read`
# to fill an unset var whenever `op` is on PATH at all, and the real `op` on
# the machine building this could satisfy those op:// references for real —
# turning a token this suite meant to leave "unset" into a live one, and a
# later "Telegram getMe" check into a real API call. Scenarios that want a
# var "set" export it directly instead, which every sourced env.sh treats as
# already-provided and never touches op for.
#
# `chezmoi`'s and `gh`'s EXPECTED calls (execute-template for the hostname
# gate, status for the sync check; auth status) get sane default stub
# behaviour everywhere below via default_chezmoi/default_gh, so an
# "unexpected call" really does mean unexpected, not "a call this suite never
# bothered to answer" — both fire on literally every run_doctor(), so leaving
# them on the loud/unanswered default made almost the whole suite exercise
# the "test bug" path instead of doctor.sh's real logic.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

for b in bash python3; do
  command -v "$b" >/dev/null 2>&1 || die "$b is not installed"
done

DOCTOR="$REPO/dot_claude/skills/linear-crud/scripts/executable_doctor.sh"
[ -f "$DOCTOR" ] || die "missing $DOCTOR — wrong REPO?"

# Must match CYCLE_RUNNER_HOST in doctor.sh itself and the plist template's
# own hostname gate.
MINI_HOST="Toms-Mac-mini"
OTHER_HOST="some-other-mac"

fails=0
ok()  { note "ok   $*"; }
bad() { err "$*"; fails=$((fails + 1)); }

bin="$WORK/bin"; mkdir -p "$bin"

stub() {  # NAME BODY — writes an executable stub onto $bin
  cat >"$bin/$1"
  chmod +x "$bin/$1"
}

# Always fails: no real op:// reference may resolve in this suite, ever.
stub op <<'STUB'
#!/usr/bin/env bash
exit 1
STUB

# Unexpected calls are a test bug, not a silent no-op — same convention as
# check-cycle-runner-policy.sh's default_gh. Used for externals with no
# "expected on every run" call of their own (claude, launchctl): each test
# that legitimately calls them installs its own stub and restores this
# afterwards.
loud_default() {
  stub "$1" <<STUB
#!/usr/bin/env bash
echo "check-doctor: unexpected $1 call: \$*" >&2
exit 97
STUB
}
loud_default claude
loud_default launchctl

# gh auth status fires on EVERY run_doctor() (doctor.sh's pre-existing "──
# auth ──" section, reused by the cycle-runner summary line) — default it to
# a boring success so only a deliberately-broken gh scenario is "unexpected".
default_gh() {
  stub gh <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2" == "auth status" ]]; then
  echo "Logged in to github.com as testuser (keyring)" >&2
  exit 0
fi
echo "check-doctor: unexpected gh call: $*" >&2
exit 97
STUB
}

# chezmoi execute-template (the hostname gate) and chezmoi status (the sync
# check) both fire on EVERY run_doctor() too. Default: reports as the mini
# (DOCTOR_TEST_HOSTNAME overrides it — the hostname-gating tests below set
# it) and a clean status.
default_chezmoi() {
  stub chezmoi <<STUB
#!/usr/bin/env bash
case "\$1" in
  execute-template) printf '%s' "\${DOCTOR_TEST_HOSTNAME:-$MINI_HOST}"; exit 0 ;;
  status) exit 0 ;;
  *) echo "check-doctor: unexpected chezmoi call: \$*" >&2; exit 97 ;;
esac
STUB
}

# launchctl list io.silverbeer.cycle-runner: sane default is "not loaded"
# (exit 1, no output) — a real machine that has never run `launchctl load`
# yet must WARN, not fail the whole suite by going unstubbed.
default_launchctl() {
  stub launchctl <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2" == "list io.silverbeer.cycle-runner" ]]; then
  exit 1
fi
echo "check-doctor: unexpected launchctl call: $*" >&2
exit 97
STUB
}

default_gh
default_chezmoi
default_launchctl

export PATH="$bin:$PATH"

# A copy of just the linear-crud skill's scripts dir, under a tree with NO
# gatekeeper skill beside it — sibling_scripts()'s "../../gatekeeper/scripts"
# lookup then finds nothing, exercising "skill absent" as a scenario
# distinct from "skill present, token unset" (SB-930 review: the token-unset
# branch used to be checked first regardless, so "skill not found" could
# never actually fire).
GK_ABSENT_ROOT="$WORK/repo-no-gatekeeper"
mkdir -p "$GK_ABSENT_ROOT/dot_claude/skills"
LINEAR_CRUD_SKILL_DIR="$(CDPATH='' cd -- "$(dirname -- "$DOCTOR")/.." && pwd)"
cp -R "$LINEAR_CRUD_SKILL_DIR" "$GK_ABSENT_ROOT/dot_claude/skills/"
GK_ABSENT_DOCTOR="$GK_ABSENT_ROOT/dot_claude/skills/linear-crud/scripts/executable_doctor.sh"
[ -f "$GK_ABSENT_DOCTOR" ] || die "gatekeeper-absent fixture missing $GK_ABSENT_DOCTOR"

# Fresh scratch $HOME per call: doctor.sh reads $HOME/.config/linear/gql-key
# (left absent -> the Linear API section fails closed without ever shelling
# out to linear-gql.sh, so no curl to the real API), $HOME/.claude/skills/*,
# $HOME/Library/LaunchAgents/io.silverbeer.cycle-runner.plist and
# $HOME/.local/state/cycle-runner/lock/pid.
run_doctor_at() {  # DOCTOR_PATH [env assignments...]
  local doctor="$1"; shift
  local h
  h="$(mktemp -d "$WORK/home.XXXXXX")"
  HOME="$h" LINEAR_KEY_FILE="$h/no-such-key" "$@" bash "$doctor" 2>&1 || true
}
run_doctor() {  # [env assignments...]
  run_doctor_at "$DOCTOR" "$@"
}

# ---------------------------------------------------- 1. OAuth token check
#
# Default hostname (DOCTOR_TEST_HOSTNAME unset -> $MINI_HOST) throughout this
# section: it exercises the on-mini branch, same as before the hostname gate
# existed. The gate itself is exercised separately below.

out="$(run_doctor)"
if [[ "$out" == *"CLAUDE_CODE_OAUTH_TOKEN not set"* ]]; then
  ok "token unset -> warns, does not attempt claude -p"
else
  bad "token-unset case did not warn as expected: $out"
fi

loud_default claude  # nothing must call it in the unset case
if [[ "$out" == *"unexpected claude call"* ]]; then
  bad "token-unset case called claude anyway"
fi

# Rejects --max-turns specifically (SB-929's run.sh already hit this exact
# gap on `claude -p`: 2.1.251 has no such flag) — a regression back to it
# must be caught here, not just by an assertion on the source text.
stub claude <<'STUB'
#!/usr/bin/env bash
case " $* " in
  *" --max-turns "*) echo "error: unknown option '--max-turns'" >&2; exit 1 ;;
esac
echo "ok, all good"
exit 0
STUB
out="$(run_doctor env CLAUDE_CODE_OAUTH_TOKEN=fake-token-for-testing)"
if [[ "$out" == *"claude OAuth token valid (ok, all good)"* ]]; then
  ok "token set + claude succeeds -> ok, echoes the reply"
else
  bad "token-set-success case did not report as expected: $out"
fi

# Budget-exceeded is NOT an auth failure (SB-942): reaching the cap means the
# call authenticated and started billing. The old $0.02 cap made this the
# outcome for every token, valid or not, and it was reported as a credential
# failure — which sent an operator hunting a bug that did not exist.
stub claude <<'STUB'
#!/usr/bin/env bash
echo "Error: Exceeded USD budget (0.10)" >&2
exit 1
STUB
out="$(run_doctor env CLAUDE_CODE_OAUTH_TOKEN=fake-token-for-testing)"
if [[ "$out" == *"probe cap"* && "$out" == *"token authenticated"* ]]; then
  ok "token set + budget exceeded -> warns, does not claim the token is bad"
else
  bad "budget-exceeded case was not reported as a cap warning: $out"
fi
if [[ "$out" == *"claude -p failed with CLAUDE_CODE_OAUTH_TOKEN set"* ]]; then
  bad "budget-exceeded case was reported as an auth failure"
fi

stub claude <<'STUB'
#!/usr/bin/env bash
echo "invalid_api_key: token expired" >&2
exit 1
STUB
out="$(run_doctor env CLAUDE_CODE_OAUTH_TOKEN=fake-token-for-testing)"
if [[ "$out" == *"claude -p failed with CLAUDE_CODE_OAUTH_TOKEN set"* && "$out" == *"token expired"* ]]; then
  ok "token set + claude fails -> fail, names the reason"
else
  bad "token-set-failure case did not report as expected: $out"
fi
loud_default claude

# ------------------------------------------------- 1b. hostname gate itself
#
# Off the mini, `claude -p` (a real, billed API call) must never even be
# attempted, no matter what CLAUDE_CODE_OAUTH_TOKEN is set to.

out="$(run_doctor env DOCTOR_TEST_HOSTNAME="$OTHER_HOST" CLAUDE_CODE_OAUTH_TOKEN=fake-token-for-testing)"
if [[ "$out" == *"not the cycle-runner host, skipping live claude -p check"* ]]; then
  ok "off-mini -> skips the claude -p check with an info line, not warn/fail"
else
  bad "off-mini case did not skip as expected: $out"
fi
if [[ "$out" == *"CLAUDE_CODE_OAUTH_TOKEN not set"* ]]; then
  bad "off-mini case still evaluated the OAuth-token branch"
fi

loud_default claude  # off-mini must never reach claude, token set or not
out="$(run_doctor env DOCTOR_TEST_HOSTNAME="$OTHER_HOST" CLAUDE_CODE_OAUTH_TOKEN=fake-token-for-testing)"
if [[ "$out" == *"unexpected claude call"* ]]; then
  bad "off-mini case called claude anyway, even with a token set"
else
  ok "off-mini + token set -> still never calls claude"
fi

default_chezmoi  # back to reporting as the mini for everything below

# --------------------------------------------------------- 2. gh auth status

stub gh <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2" == "auth status" ]]; then
  echo "Logged in to github.com as testuser (keyring)" >&2
  exit 0
fi
echo "check-doctor: unexpected gh call: $*" >&2
exit 97
STUB
out="$(run_doctor)"
if [[ "$out" == *"gh auth status: Logged in to github.com as testuser"* ]]; then
  ok "gh auth status ok -> the one-line summary is surfaced"
else
  bad "gh-ok case did not report as expected: $out"
fi

stub gh <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2" == "auth status" ]]; then
  echo "You are not logged into any GitHub hosts" >&2
  exit 1
fi
echo "check-doctor: unexpected gh call: $*" >&2
exit 97
STUB
out="$(run_doctor)"
if [[ "$out" == *"gh auth status failed"* && "$out" == *"not logged into any GitHub hosts"* ]]; then
  ok "gh auth status fails -> warn, names the reason"
else
  bad "gh-fail case did not report as expected: $out"
fi
default_gh

# ------------------------------------------------------------ 3. Telegram
#
# Skill absent must report distinctly from skill present + token unset (the
# unreachable-branch fix): checked first below so a regression back to
# checking the token first is caught even if both messages happen to be
# present in the same output for some other reason.

out="$(run_doctor_at "$GK_ABSENT_DOCTOR")"
if [[ "$out" == *"gatekeeper skill not found"* ]]; then
  ok "gatekeeper skill absent -> warns 'skill not found'"
else
  bad "gatekeeper-skill-absent case did not report as expected: $out"
fi
if [[ "$out" == *"GATEKEEPER_TG_TOKEN not set"* ]]; then
  bad "gatekeeper-skill-absent case ALSO reported the token-unset message — the skill-presence check must win"
fi

out="$(run_doctor)"
if [[ "$out" == *"GATEKEEPER_TG_TOKEN not set"* && "$out" == *"skipping Telegram getMe"* ]]; then
  ok "GATEKEEPER_TG_TOKEN unset (skill present) -> skips cleanly (warn, not fail)"
else
  bad "telegram-token-unset case did not report as expected: $out"
fi
if [[ "$out" == *"gatekeeper skill not found"* ]]; then
  bad "skill-present-token-unset case ALSO reported 'skill not found'"
fi

# ------------------------------------------------------- 4. stale-lock check

out="$(run_doctor)"
if [[ "$out" == *"no cycle-runner lock present"* ]]; then
  ok "no lock dir at all -> ok, informational"
else
  bad "no-lock case did not report as expected: $out"
fi

run_doctor_with_lock() {  # PID
  local h
  h="$(mktemp -d "$WORK/home.XXXXXX")"
  mkdir -p "$h/.local/state/cycle-runner/lock"
  echo "$1" >"$h/.local/state/cycle-runner/lock/pid"
  HOME="$h" LINEAR_KEY_FILE="$h/no-such-key" bash "$DOCTOR" 2>&1 || true
}

out="$(run_doctor_with_lock "$$")"
if [[ "$out" == *"cycle-runner lock held by running pid $$"* ]]; then
  ok "lock held by a running pid (this test's own \$\$) -> ok"
else
  bad "running-lock case did not report as expected: $out"
fi

( : ) & dead_pid=$!; wait "$dead_pid" 2>/dev/null || true
out="$(run_doctor_with_lock "$dead_pid")"
if [[ "$out" == *"pid '$dead_pid' is not running"*"looks abandoned"* ]]; then
  ok "lock names a dead pid -> warn, 'looks abandoned', not auto-cleared"
else
  bad "stale-lock case did not report as expected: $out"
fi

# ---------------------------------------------- 5. cycle-runner plist check
#
# ON THE MINI ONLY. Guards the hostname-drift scenario: an OS reinstall or
# rename that stops chezmoi from ever deploying the plist again, while every
# other check stays green.

run_doctor_with_plist() {  # PRESENT(0/1) [env assignments...]
  local present="$1"; shift
  local h
  h="$(mktemp -d "$WORK/home.XXXXXX")"
  if [ "$present" -eq 1 ]; then
    mkdir -p "$h/Library/LaunchAgents"
    : >"$h/Library/LaunchAgents/io.silverbeer.cycle-runner.plist"
  fi
  HOME="$h" LINEAR_KEY_FILE="$h/no-such-key" "$@" bash "$DOCTOR" 2>&1 || true
}

out="$(run_doctor_with_plist 0)"
if [[ "$out" == *"cycle-runner plist missing at"* ]]; then
  ok "on-mini + plist absent -> fail, names the missing path"
else
  bad "plist-missing case did not report as expected: $out"
fi

default_launchctl  # not loaded, by default
out="$(run_doctor_with_plist 1)"
if [[ "$out" == *"cycle-runner plist present at"* && "$out" == *"cycle-runner plist not loaded in launchctl"* ]]; then
  ok "on-mini + plist present, not loaded -> ok (present) + warn (not loaded), not fail"
else
  bad "plist-present-not-loaded case did not report as expected: $out"
fi

stub launchctl <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2" == "list io.silverbeer.cycle-runner" ]]; then
  exit 0
fi
echo "check-doctor: unexpected launchctl call: $*" >&2
exit 97
STUB
out="$(run_doctor_with_plist 1)"
if [[ "$out" == *"cycle-runner plist present at"* && "$out" == *"cycle-runner plist loaded in launchctl"* ]]; then
  ok "on-mini + plist present, loaded -> ok, ok"
else
  bad "plist-present-loaded case did not report as expected: $out"
fi
default_launchctl

loud_default launchctl  # off-mini must never even query launchd
out="$(run_doctor_with_plist 1 env DOCTOR_TEST_HOSTNAME="$OTHER_HOST")"
if [[ "$out" == *"not the cycle-runner host, skipping cycle-runner plist deployment check"* ]]; then
  ok "off-mini -> skips the plist check with an info line, not warn/fail"
else
  bad "off-mini plist case did not skip as expected: $out"
fi
if [[ "$out" == *"unexpected launchctl call"* ]]; then
  bad "off-mini plist case queried launchctl anyway"
fi
default_launchctl
default_chezmoi

# ------------------------------------------------------- 6. chezmoi status

stub chezmoi <<STUB
#!/usr/bin/env bash
case "\$1" in
  execute-template) printf '%s' "\${DOCTOR_TEST_HOSTNAME:-$MINI_HOST}"; exit 0 ;;
  status) exit 0 ;;
  *) echo "check-doctor: unexpected chezmoi call: \$*" >&2; exit 97 ;;
esac
STUB
out="$(run_doctor)"
if [[ "$out" == *"chezmoi status clean — safe to re-add"* ]]; then
  ok "chezmoi status clean (no output, exit 0) -> ok"
else
  bad "chezmoi-clean case did not report as expected: $out"
fi

stub chezmoi <<STUB
#!/usr/bin/env bash
case "\$1" in
  execute-template) printf '%s' "\${DOCTOR_TEST_HOSTNAME:-$MINI_HOST}"; exit 0 ;;
  status) echo "MM  .claude/CLAUDE.md"; exit 0 ;;
  *) echo "check-doctor: unexpected chezmoi call: \$*" >&2; exit 97 ;;
esac
STUB
out="$(run_doctor)"
if [[ "$out" == *"chezmoi status is NOT clean"* && "$out" == *"PR #37 silently reverted PR #35"* ]]; then
  ok "chezmoi status dirty -> fail, names the PR #37/#35 incident"
else
  bad "chezmoi-dirty case did not report as expected: $out"
fi

stub chezmoi <<STUB
#!/usr/bin/env bash
case "\$1" in
  execute-template) printf '%s' "\${DOCTOR_TEST_HOSTNAME:-$MINI_HOST}"; exit 0 ;;
  status) echo "chezmoi: onepasswordRead: exit status 1: vault is locked" >&2; exit 1 ;;
  *) echo "check-doctor: unexpected chezmoi call: \$*" >&2; exit 97 ;;
esac
STUB
out="$(run_doctor)"
if [[ "$out" == *"chezmoi status failed to run"* && "$out" == *"vault is locked"* ]]; then
  ok "chezmoi status itself errors (locked vault) -> fail, distinct from 'not clean'"
else
  bad "chezmoi-status-errors case did not report as expected: $out"
fi
default_chezmoi

# ---------------------------------------------------------------- verdict

[ "$fails" -eq 0 ] || die "check-doctor: $fails failure(s)"
note "check-doctor: all offline tests passed"
