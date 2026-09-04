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

# kubectl: sane default is "the CronJob is not there" (exit 1), the state of
# every machine that has not applied k3s/cycle-runner/ yet.
#
# This stub is NOT optional. Without it doctor.sh finds the developer's REAL
# kubectl and queries their live cluster on every one of the ~15 doctor
# invocations this suite makes — measured at 2m53s, and a suite that talks to a
# real cluster is not an offline suite at all.
default_kubectl() {
  stub kubectl <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
}

default_gh
default_chezmoi
default_launchctl
default_kubectl

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
# and $HOME/Library/LaunchAgents/io.silverbeer.cycle-runner.plist.
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

# SB-974 supersedes SB-972: doctor must not call `op` AT ALL, with or without a
# token. SB-972 kept `op whoami` for the service-account case; that still
# reaches 1Password, and once a desktop account exists `op` prefers it and can
# wedge on an unanswered prompt — blocking every later call and hanging the
# machine doctor is supposed to be diagnosing.
OP_CALLED_MARKER="$WORK/op-was-called"; export OP_CALLED_MARKER
stub op <<'STUB'
#!/usr/bin/env bash
: >"$OP_CALLED_MARKER"
echo "op should not have been called"
exit 0
STUB

for scenario in "with a token:OP_SERVICE_ACCOUNT_TOKEN=fake-service-token" "without a token:"; do
  label="${scenario%%:*}"; extra="${scenario#*:}"
  rm -f "$OP_CALLED_MARKER"
  if [ -n "$extra" ]; then
    out="$(run_doctor env "$extra" CLAUDE_CODE_OAUTH_TOKEN=fake-token-for-testing)"
  else
    out="$(run_doctor env -u OP_SERVICE_ACCOUNT_TOKEN CLAUDE_CODE_OAUTH_TOKEN=fake-token-for-testing)"
  fi
  if [ -e "$OP_CALLED_MARKER" ]; then
    bad "doctor invoked op $label — it must never reach 1Password (SB-974)"
  else
    ok "op: doctor makes no 1Password call $label"
  fi
done

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

# ------------------------------------------- 4. CronJob health (SB-976)
#
# Replaces the stale-lock scenarios. The lock is gone, so doctor's read of it
# is too; what it reads instead is whether the scheduler is scheduling.
#
# Every case runs with a STUBBED kubectl. A real one would talk to whatever
# cluster the runner of this suite happens to have — including none, on CI.

kubectl_stub() {  # SCRIPT-BODY
  cat >"$bin/kubectl" <<STUB
#!/usr/bin/env bash
$1
STUB
  chmod +x "$bin/kubectl"
}

# 4a. kubectl absent -> INFO. This doctor also runs on the Air, where there is
# no cluster and nothing to say about one.
#
# Removing the stub is NOT enough: the machine running this suite very likely
# has a real kubectl further down $PATH, and doctor would then query its actual
# cluster — measured, it reported on a live k3s from a laptop.
#
# Trimming $PATH does not work either: GitHub's ubuntu runner ships a kubectl
# in /usr/bin, so on CI — the one place this must hold — the case proved
# nothing and the guard for it failed the build instead.
#
# So doctor takes the binary name from $KUBECTL, and this points it at one that
# cannot exist. The seam is real, not test-only: a machine with kubectl outside
# $PATH can use it too.
out="$(run_doctor env KUBECTL=kubectl-definitely-not-installed)"
if [[ "$out" == *"kubectl not installed, skipping the cycle-runner CronJob check"* ]]; then
  ok "no kubectl -> info, not a failure"
else
  bad "kubectl-absent case did not report as expected: $out"
fi

# 4b. healthy: scheduled, not suspended.
kubectl_stub 'case "$*" in
  *"get cronjob cycle-runner -n cycle-runner -o jsonpath"*)
    printf "false\t2026-09-02T11:30:00Z\t2026-09-02T11:31:12Z" ;;
  *"get cronjob"*) exit 0 ;;
  *) exit 1 ;;
esac'
out="$(run_doctor)"
if [[ "$out" == *"CronJob last scheduled 2026-09-02T11:30:00Z"* && "$out" == *"last success 2026-09-02T11:31:12Z"* ]]; then
  ok "healthy CronJob -> ok, names the last schedule and last success"
else
  bad "healthy-CronJob case did not report as expected: $out"
fi

# 4c. SUSPENDED. This is the modern shape of the silence launchd used to fail
# in: everything else green, and no tick has run for days.
kubectl_stub 'case "$*" in
  *"get cronjob cycle-runner -n cycle-runner -o jsonpath"*)
    printf "true\t2026-09-01T11:30:00Z\t2026-09-01T11:31:00Z" ;;
  *"get cronjob"*) exit 0 ;;
  *) exit 1 ;;
esac'
out="$(run_doctor)"
if [[ "$out" == *"CronJob is SUSPENDED"* && "$out" == *"suspend\":false"* ]]; then
  ok "suspended CronJob -> fail, with the unsuspend command"
else
  bad "suspended-CronJob case did not report as expected: $out"
fi

# 4d. applied but never scheduled -> warn, not fail. Right after an apply this
# is the normal state for up to half an hour.
kubectl_stub 'case "$*" in
  *"get cronjob cycle-runner -n cycle-runner -o jsonpath"*) printf "false\t\t" ;;
  *"get cronjob"*) exit 0 ;;
  *) exit 1 ;;
esac'
out="$(run_doctor)"
if [[ "$out" == *"has never been scheduled"* ]]; then
  ok "never-scheduled CronJob -> warn, not fail"
else
  bad "never-scheduled case did not report as expected: $out"
fi

# 4e. kubectl present, CronJob absent -> warn with the apply command.
kubectl_stub 'exit 1'
out="$(run_doctor)"
if [[ "$out" == *"no cycle-runner CronJob in the cluster"* && "$out" == *"kubectl apply -f k3s/cycle-runner/"* ]]; then
  ok "missing CronJob -> warn, with the apply command"
else
  bad "missing-CronJob case did not report as expected: $out"
fi
# Back to the default stub: every doctor run after this section would
# otherwise find the machine's real kubectl again.
default_kubectl

# ------------------------------------- 4f. claude drift, image vs machine
#
# Two runtimes now exist and drift independently (SB-978). A behaviour
# difference between an interactive /work and a headless tick is otherwise
# unattributable.

# `claude` is stubbed per case so the machine's real version cannot leak in.
claude_stub() {  # VERSION
  cat >"$bin/claude" <<STUB
#!/usr/bin/env bash
if [ "\$1" = "--version" ]; then echo "$1 (Claude Code)"; exit 0; fi
echo "Ready. What's the task?"
STUB
  chmod +x "$bin/claude"
}
cj_image_stub() {  # IMAGE
  cat >"$bin/kubectl" <<STUB
#!/usr/bin/env bash
case "\$*" in
  *"jsonpath={.spec.jobTemplate.spec.template.spec.containers[0].image}"*) printf '%s' "$1" ;;
  *"get cronjob cycle-runner -n cycle-runner -o jsonpath"*) printf "false\\t2026-09-02T11:30:00Z\\t2026-09-02T11:31:12Z" ;;
  *"get cronjob"*) exit 0 ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$bin/kubectl"
}

claude_stub 2.1.258
cj_image_stub ghcr.io/silverbeer/cycle-runner:claude-2.1.258
out="$(run_doctor)"
if [[ "$out" == *"claude 2.1.258 in the image and on this machine — no drift"* ]]; then
  ok "same claude in image and machine -> ok, no drift"
else
  bad "no-drift case did not report as expected: $out"
fi

cj_image_stub ghcr.io/silverbeer/cycle-runner:claude-2.1.300
out="$(run_doctor)"
if [[ "$out" == *"claude drift: the image runs 2.1.300, this machine runs 2.1.258"* ]]; then
  ok "different claude in image and machine -> warn, names both versions"
else
  bad "drift case did not report as expected: $out"
fi

# :latest is a warning of its own: nothing to compare, and a rebuild lands
# unreviewed.
cj_image_stub ghcr.io/silverbeer/cycle-runner:latest
out="$(run_doctor)"
if [[ "$out" == *"the CronJob runs :latest"* ]]; then
  ok ":latest image -> warn, no version to compare"
else
  bad ":latest case did not report as expected: $out"
fi

default_kubectl
# Back to the loud default: an unexpected `claude` call after this section is a
# test bug, not a silent no-op.
loud_default claude

# ------------------------------------- 4g. cluster health (SB-1001)
#
# The generic version of a check written four times for four specific things:
# SB-953, SB-980, SB-987, SB-1000. Each was "something is not running and
# nothing says so", found by a human looking for something else.

# A kubectl that answers the four cluster queries from fixtures. Anything not
# stubbed here is an unexpected call and fails loudly.
cluster_stub() {  # PODS DEPLOYS CRONJOBS CPU_PCT
  cat >"$bin/kubectl" <<STUB
#!/usr/bin/env bash
case "\$*" in
  *"get pods -A"*)     printf '%s' "$1" ;;
  *"get deploy -A"*)   printf '%s' "$2" ;;
  *"get cronjob -A -o jsonpath"*) printf '%s' "$3" ;;
  *"describe node"*)   printf 'Allocated resources:\\n  cpu  1900m ($4%%)  5400m (270%%)\\nEvents:\\n' ;;
  *"get cronjob cycle-runner -n cycle-runner -o jsonpath"*)
    printf "false\\t2026-09-04T11:00:00Z\\t2026-09-04T11:00:12Z" ;;
  *"jsonpath={.spec.jobTemplate"*) printf 'ghcr.io/silverbeer/cycle-runner:claude-2.1.258' ;;
  *"get cronjob"*) exit 0 ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$bin/kubectl"
}

HEALTHY_PODS=""
HEALTHY_DEPLOYS="cycle-runner  runner  1/1  1  1  1d"
HEALTHY_CRONS=$'cycle-runner/cycle-runner\tfalse\t2026-09-04T11:00:00Z\t2026-01-01T00:00:00Z'

# 4g1. an ImagePullBackOff is named, with its namespace and reason. 232 days
# of this went unreported (SB-1000).
cluster_stub "missing-table  missing-table-backend  0/1  ImagePullBackOff  0  232d" \
             "$HEALTHY_DEPLOYS" "$HEALTHY_CRONS" 67
out="$(run_doctor)"
if [[ "$out" == *"neither Running nor Succeeded"* && "$out" == *"missing-table/missing-table-backend"* \
      && "$out" == *"ImagePullBackOff"* ]]; then
  ok "an unhealthy pod -> warn, named with its namespace and reason"
else
  bad "unhealthy-pod case did not report as expected: $out"
fi

# 4g2. THE ONE THAT MATTERS MOST. A deployment scaled to 0 on purpose is
# healthy. SB-1000 parks a dead stack exactly that way, and a check that nags
# about a deliberate state is a check that gets turned off.
cluster_stub "$HEALTHY_PODS" \
             "missing-table  missing-table-backend  0/0  0  0  232d" "$HEALTHY_CRONS" 67
out="$(run_doctor)"
if [[ "$out" == *"below desired replicas"* ]]; then
  bad "a deliberately scaled-to-zero deployment was reported as unhealthy: $out"
else
  ok "a deployment scaled to 0 on purpose -> silent, not a warning"
fi

# 4g3. ...and the complement: ready < desired IS a finding.
cluster_stub "$HEALTHY_PODS" "iron-claw  freeradius  0/1  1  0  192d" "$HEALTHY_CRONS" 67
out="$(run_doctor)"
if [[ "$out" == *"below desired replicas"* && "$out" == *"iron-claw/freeradius"* ]]; then
  ok "a deployment below desired replicas -> warn, named"
else
  bad "below-desired case did not report as expected: $out"
fi

# 4g4. A CronJob that has NEVER scheduled — SB-987 exactly. A "last run
# failed" check cannot see this: there is no run to fail.
cluster_stub "$HEALTHY_PODS" "$HEALTHY_DEPLOYS" \
             $'cycle-runner/triage\tfalse\t\t2026-01-01T00:00:00Z' 67
out="$(run_doctor)"
if [[ "$out" == *"never been scheduled"* && "$out" == *"cycle-runner/triage"* ]]; then
  ok "a CronJob that has never scheduled -> warn (the SB-987 shape)"
else
  bad "never-scheduled case did not report as expected: $out"
fi

# 4g4b. ...but a CronJob created an hour ago has not scheduled YET, and is not
# broken. trd-engine-report was flagged 11 hours after creation, before its
# first slot — a check that fires on a healthy new job is one someone turns
# off.
cluster_stub "$HEALTHY_PODS" "$HEALTHY_DEPLOYS" \
             $'trd/trd-engine-report\tfalse\t\t'"$(date -u '+%Y-%m-%dT%H:%M:%SZ')" 67
out="$(run_doctor)"
if [[ "$out" == *"never been scheduled"* ]]; then
  bad "a CronJob created today was reported as never having run: $out"
else
  ok "a newly created CronJob -> silent until it has had a fair chance"
fi

# 4g5. CPU pressure, reported BEFORE the next pod fails to schedule. This is
# what forced SB-981's fictional 50m request.
cluster_stub "$HEALTHY_PODS" "$HEALTHY_DEPLOYS" "$HEALTHY_CRONS" 95
out="$(run_doctor)"
if [[ "$out" == *"CPU requests at 95%"* && "$out" == *"will not schedule"* ]]; then
  ok "CPU requests over the threshold -> warn, before the next pod fails"
else
  bad "cpu-pressure case did not report as expected: $out"
fi

# 4g6. All healthy -> positive confirmation, not silence. A check that only
# ever speaks on failure cannot be distinguished from one that is broken.
cluster_stub "$HEALTHY_PODS" "$HEALTHY_DEPLOYS" "$HEALTHY_CRONS" 67
out="$(run_doctor)"
if [[ "$out" == *"every pod in the cluster is Running or Succeeded"* \
      && "$out" == *"CPU requests at 67%"* ]]; then
  ok "a healthy cluster -> says so, rather than staying silent"
else
  bad "healthy-cluster case did not report as expected: $out"
fi

default_kubectl

# ------------------------------- 5. the cutover check, inverted (SB-979)
#
# The cycle-runner is a k3s CronJob and its plist is deleted. A LOADED agent is
# now a failure: run.sh's own lock was removed in SB-976 on the strength of
# `concurrencyPolicy: Forbid`, which governs only the CronJob's own jobs — so
# two schedulers would race for the same tickets, gates and merge queue with
# nothing at all stopping them.
#
# That is exactly what a stale `chezmoi update` on a second Mac used to
# produce, which is why this is checked on every host rather than the mini.

# 5a. loaded -> FAIL, and say what to run.
stub launchctl <<'STUB'
#!/usr/bin/env bash
[[ "$1 $2" == "list io.silverbeer.cycle-runner" ]] && exit 0
echo "check-doctor: unexpected launchctl call: $*" >&2
exit 97
STUB
out="$(run_doctor)"
if [[ "$out" == *"launchd agent is LOADED"* && "$out" == *"launchctl unload -w"* ]]; then
  ok "a loaded cycle-runner agent -> fail, with the unload command"
else
  bad "loaded-agent case did not report as expected: $out"
fi

# 5b. not loaded, no file -> the healthy post-cutover state.
default_launchctl
out="$(run_doctor)"
if [[ "$out" == *"no cycle-runner launchd agent"* ]]; then
  ok "no agent and no plist -> ok, the CronJob is the only scheduler"
else
  bad "clean-cutover case did not report as expected: $out"
fi

# 5c. not loaded but the file is still on disk -> warn, not fail. Harmless
# today; a loaded gun for whoever runs `launchctl load` without reading.
plist_home="$(mktemp -d "$WORK/home.XXXXXX")"
mkdir -p "$plist_home/Library/LaunchAgents"
: >"$plist_home/Library/LaunchAgents/io.silverbeer.cycle-runner.plist"
out="$(HOME="$plist_home" LINEAR_KEY_FILE="$plist_home/no-such-key" bash "$DOCTOR" 2>&1 || true)"
if [[ "$out" == *"leftover cycle-runner plist"* ]]; then
  ok "an unloaded leftover plist -> warn, with the rm"
else
  bad "leftover-plist case did not report as expected: $out"
fi

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
