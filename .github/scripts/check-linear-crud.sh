#!/usr/bin/env bash
# Offline functional tests for the linear-crud and backlog-groom scripts.
#
# Everything here runs against a SCRATCH COPY of dot_claude/skills with no
# network: `linear` and `curl` are shadowed by stubs that fail loudly and leave
# a marker, linear-gql.sh is shadowed by a fixture-returning stub (the
# scripts resolve the unprefixed name first, so the stub wins over the real
# executable_linear-gql.sh), and `gh` answers as it would with no PR.
#
#   1. repo_label / epic_repo table against repos.json      (bash, inline)
#   2. the `stats` jq program against a fixture             (bash, inline)
#   3. `linear.sh pack` output schema inside a temp git repo (bash, inline)
#   4-6. board.py graph helpers, linear_api.warn_if_capped,
#        apply.py plan() idempotence                (.github/tests/linear_crud)
#
# linear.sh is sourced: it returns before its dispatcher when BASH_SOURCE != $0,
# so the functions are defined without running a subcommand.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

for b in jq python3 git; do
  command -v "$b" >/dev/null 2>&1 || die "$b is not installed"
done

skills_src="$REPO/dot_claude/skills"
[ -f "$skills_src/linear-crud/scripts/linear.sh" ] || die "missing $skills_src/linear-crud/scripts/linear.sh — wrong REPO?"
[ -f "$skills_src/backlog-groom/scripts/apply.py" ] || die "missing $skills_src/backlog-groom/scripts/apply.py — wrong REPO?"
tests_dir="$REPO/.github/tests/linear_crud"
[ -d "$tests_dir" ] || die "missing $tests_dir"

# ------------------------------------------------------------ scratch setup

SKILLS="$WORK/skills"
rm -rf "$SKILLS"; mkdir -p "$SKILLS"
cp -R "$skills_src/linear-crud" "$skills_src/backlog-groom" "$SKILLS/"
SCRIPTS="$SKILLS/linear-crud/scripts"
LINEAR_SH="$SCRIPTS/linear.sh"
REPOS_JSON="$SKILLS/linear-crud/repos.json"

# No CLI, no network — whatever LINEAR_API_KEY says. A stub that gets called
# is a test failure: it means a code path reached for Linear that these tests
# believe is offline. The stub fails loudly AND drops a marker, because a
# caller that swallows the stub's exit code would otherwise hide the call.
NET_MARKER="$WORK/network-was-called"; rm -f "$NET_MARKER"; export NET_MARKER
bin="$WORK/bin"; mkdir -p "$bin"
for b in linear curl; do
  # shellcheck disable=SC2016  # $* and $NET_MARKER expand inside the stub, not here
  printf '#!/usr/bin/env bash\necho "%s $*" >>"$NET_MARKER"\necho "check-linear-crud: %s was called — these tests must be offline" >&2\nexit 97\n' "$b" "$b" >"$bin/$b"
  chmod +x "$bin/$b"
done
# pack passes --jq to gh, so `null` is exactly what gh prints when the branch
# has no PR.
printf '#!/usr/bin/env bash\necho null\n' >"$bin/gh"; chmod +x "$bin/gh"
export PATH="$bin:$PATH"

GQL_LOG="$WORK/gql.log"; : >"$GQL_LOG"; export GQL_LOG
cat >"$SCRIPTS/linear-gql.sh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "${1:-$(cat)}" >>"$GQL_LOG"
cat <<'JSON'
{"data":{"issue":{"identifier":"SB-1","title":"Add board: delivery view (v2)","state":{"name":"Todo"},"estimate":2,"priority":3,"labels":{"nodes":[{"name":"DOT"},{"name":"feature"}]},"url":"https://linear.app/silverbeer/issue/SB-1"}}}
JSON
STUB

fails=0
ok()  { note "ok   $*"; }
bad() { err "$*"; fails=$((fails + 1)); }

# The functions under test come from sourcing the scratch linear.sh, which
# returns before its dispatcher when sourced. Prove that guard still holds
# before relying on it: sourcing must define the functions and print nothing.
fn_lib="$LINEAR_SH"
for f in die repos_json load_repo_rules match_repo_glob repo_label epic_repo branch_name_for stats_jq; do
  grep -q "^$f()" "$LINEAR_SH" || die "linear.sh no longer defines $f() — update this check"
done
if ! src_out="$(bash -c '. "$1"; declare -F repo_label stats_jq >/dev/null && echo sourced' _ "$fn_lib" 2>&1)" \
   || [ "$src_out" != "sourced" ]; then
  die "sourcing linear.sh did not stop at its source guard: ${src_out:-<no output>}"
fi

# ----------------------------------------------- 1. repo_label / epic_repo

note "1. repo_label / epic_repo against $REPOS_JSON"
if ! msg="$(bash -c '. "$1"; load_repo_rules' _ "$fn_lib" 2>&1)"; then
  bad "repos.json could not be loaded: $msg"
fi

# judge WHAT NAME WANT GOT RC — WANT is a label, or `exit1` for "no match".
# A miss is exit 1 with nothing on either stream; die() is exit 1 WITH a
# message, and must not pass as a miss.
judge() {
  local what="$1" name="$2" want="$3" got="$4" rc="$5" stderr
  stderr="$(cat "$WORK/stderr" 2>/dev/null || true)"
  if [ "$want" = "exit1" ]; then
    if [ "$rc" -eq 1 ] && [ -z "$got" ] && [ -z "$stderr" ]; then
      ok "$what '$name' -> no match (exit 1)"
    else
      bad "$what '$name': expected no match (exit 1), got '${got}' exit $rc ${stderr:+— $stderr}"
    fi
  elif [ "$rc" -eq 0 ] && [ "$got" = "$want" ]; then
    ok "$what '$name' -> $got"
  else
    bad "$what '$name': expected $want, got '${got}' (exit $rc) ${stderr:+— $stderr}"
  fi
}

# repo_label reads the git toplevel, falling back to cwd. GIT_DIR points at
# nothing so the scratch dir's own parent repository (if any) cannot win.
repo_case() {
  local name="$1" want="$2" got rc
  mkdir -p "$WORK/names/$name"; : >"$WORK/stderr"
  got="$( (cd "$WORK/names/$name" && GIT_DIR="$WORK/no-such-git" bash -c '. "$1"; repo_label' _ "$fn_lib") 2>"$WORK/stderr")" && rc=0 || rc=$?
  judge repo_label "$name" "$want" "$got" "$rc"
}
epic_case() {
  local name="$1" want="$2" got rc
  got="$(bash -c '. "$1"; epic_repo "$2"' _ "$fn_lib" "$name" 2>"$WORK/stderr")" && rc=0 || rc=$?
  judge epic_repo "$name" "$want" "$got" "$rc"
}

# Order matters in repos.json: bet-collect must beat bet-*, and the bootstrap
# glob must beat nothing else. Every label has at least one row.
repo_case bet-collect                     BETC
repo_case bet-capture                     BETC
repo_case bet                             BET
repo_case bet-foo                         BET
repo_case sports-betting                  BET
repo_case myrunstreak.run                 STK
repo_case runstreak-api                   STK
repo_case missing-table                   MT
repo_case podtelemetry.com                POD
repo_case pod                             POD
repo_case missing-table-android           MTA
repo_case trd                             TRD
repo_case my-investment-tools             TRD
repo_case missingtable-platform-bootstrap BOOT
repo_case foo-bootstrap                   BOOT
repo_case match-scraper                   MS
repo_case match-scraper-agent             MSA
repo_case qualityplaybook.dev             QB
repo_case qb                              QB
repo_case janitor                         JT
repo_case dotfiles                        DOT
repo_case todo                            TODO
repo_case nonexistent                     exit1
repo_case bets                            exit1

epic_case "BET — Acquisition & Capture"   BETC
epic_case "BET Odds Engine"               BET
epic_case "Paper AI Proof of Concept"     MT
epic_case "Quality & CI Automation"       STK
epic_case "Multi-metric goals"            STK
epic_case "Podtelemetry v2"               POD
epic_case "Android widgets"               MTA
epic_case "TRD backtesting"               TRD
epic_case "Unknown Thing"                 exit1

# ------------------------------------------------------- 2. stats jq program

note "2. stats jq program against an 8-issue fixture"
stats_fixture="$WORK/stats.json"
jq -n '{nodes: [
  {identifier:"SB-1", createdAt:"2026-01-01T00:00:00.000Z", updatedAt:"2026-01-05T00:00:00.000Z",
   state:{type:"completed"}, labels:{nodes:[{name:"BET"},{name:"feature"}]}, project:{name:"BET Odds Engine"}},
  {identifier:"SB-2", createdAt:"2026-01-02T00:00:00.000Z", updatedAt:"2026-01-02T00:00:00.000Z",
   state:{type:"started"},   labels:{nodes:[{name:"BET"}]},  project:{name:"BET Odds Engine"}},
  {identifier:"SB-3", createdAt:"2026-01-03T00:00:00.000Z", updatedAt:"2026-01-03T00:00:00.000Z",
   state:{type:"backlog"},   labels:{nodes:[{name:"BET"}]},  project:null},
  {identifier:"SB-4", createdAt:"2026-01-04T00:00:00.000Z", updatedAt:"2026-01-06T00:00:00.000Z",
   state:{type:"completed"}, labels:{nodes:[{name:"BETC"}]}, project:{name:"BET — Acquisition & Capture"}},
  {identifier:"SB-5", createdAt:"2026-01-05T00:00:00.000Z", updatedAt:"2026-01-05T00:00:00.000Z",
   state:{type:"unstarted"}, labels:{nodes:[{name:"BETC"},{name:"bug"}]}, project:{name:"BET — Acquisition & Capture"}},
  {identifier:"SB-6", createdAt:"2026-01-06T00:00:00.000Z", updatedAt:"2026-01-07T00:00:00.000Z",
   state:{type:"completed"}, labels:{nodes:[{name:"STK"}]},  project:{name:"Quality & CI Automation"}},
  {identifier:"SB-7", createdAt:"2026-01-07T00:00:00.000Z", updatedAt:"2026-01-07T00:00:00.000Z",
   state:{type:"canceled"},  labels:{nodes:[{name:"DOT"},{name:"chore"}]}, project:null},
  {identifier:"SB-8", createdAt:"2026-01-08T00:00:00.000Z", updatedAt:"2026-01-08T00:00:00.000Z",
   state:{type:"triage"},    labels:{nodes:[{name:"feature"}]}, project:null}
]}' >"$stats_fixture"

if stats_out="$(bash -c '. "$1"
    repos="$(jq -c "map(.label)" "$2")"
    jq -r --argjson win 7 --arg team SB --argjson repos "$repos" "$(stats_jq)" "$3"' \
    _ "$fn_lib" "$REPOS_JSON" "$stats_fixture" 2>&1)"; then
  by_repo="$(printf '%s\n' "$stats_out" | grep 'By repo:' || true)"
  # WHAT is the expected fragment, LINE the line it must sit on.
  stats_expect() {
    if printf '%s\n' "$2" | grep -qF -- "$1"; then ok "stats: $1"
    else bad "stats: expected '$1' in: ${2:-<no such line>}"; fi
  }
  # LABEL N LINE — bounded on both sides, so `BET 3` cannot be satisfied by
  # `BETC 2` or by `BET 30`.
  stats_repo() {
    if printf '%s\n' "$3" | grep -qE "(^| )$1 $2( |\$)"; then ok "stats: $1 $2"
    else bad "stats: expected '$1 $2' in: ${3:-<no such line>}"; fi
  }
  stats_repo BET  3 "$by_repo"
  stats_repo BETC 2 "$by_repo"
  stats_repo STK  1 "$by_repo"
  stats_repo DOT  1 "$by_repo"
  for l in chore feature bug; do
    if printf '%s\n' "$by_repo" | grep -qw "$l"; then bad "stats: non-repo label '$l' counted as a repo: $by_repo"; fi
  done
  stats_expect 'Shipped     3'   "$(printf '%s\n' "$stats_out" | grep 'Shipped' || true)"
  stats_expect 'Open        4   (1 in progress · 2 todo · 1 backlog)' "$(printf '%s\n' "$stats_out" | grep 'Open' || true)"
  stats_expect 'Canceled    1'   "$(printf '%s\n' "$stats_out" | grep 'Canceled' || true)"
  stats_expect 'Total ever  8'   "$(printf '%s\n' "$stats_out" | grep 'Total ever' || true)"
  stats_expect 'BET Odds Engine 2' "$(printf '%s\n' "$stats_out" | grep 'By epic:' || true)"
else
  bad "stats: jq program failed: $stats_out"
fi

# ------------------------------------------------------------ 3. pack schema

note "3. linear.sh pack SB-1 inside a temp git repo (gql + gh stubbed)"
# Named bet-collect so repoLabel exercises the dirGlobs lookup too.
prepo="$WORK/repos/bet-collect"
mkdir -p "$prepo"
git -c init.defaultBranch=main init -q "$prepo"
printf 'hello\n' >"$prepo/README.md"
git -C "$prepo" add README.md
git -C "$prepo" -c user.name=ci -c user.email=ci@example.invalid commit -q -m init

pack_field() {  # LABEL JQ-EXPR — the expression must be true
  if jq -e "$2" >/dev/null <<<"$pack"; then ok "pack: $1"
  else bad "pack: $1 — expected ($2) in: $pack"; fi
}

if pack="$(cd "$prepo" && bash "$LINEAR_SH" pack SB-1 2>"$WORK/stderr")"; then
  pack_field 'keys are issue,branchName,repoLabel,git,pr' \
    '(keys|sort) == ["branchName","git","issue","pr","repoLabel"]'
  pack_field 'branchName is silverbeer/sb-1-<slug>' \
    '.branchName == "silverbeer/sb-1-add-board-delivery-view-v2"'
  pack_field 'pr is null'            '.pr == null'
  pack_field 'git.dirty is boolean false on a clean tree' '.git.dirty == false and (.git.dirty|type) == "boolean"'
  pack_field 'git.branch is main'    '.git.branch == "main"'
  pack_field 'repoLabel from the repo dir name' '.repoLabel == "BETC"'
  pack_field 'issue is the brief projection' \
    '.issue == {identifier:"SB-1",title:"Add board: delivery view (v2)",state:"Todo",estimate:2,priority:3,labels:["DOT","feature"],url:"https://linear.app/silverbeer/issue/SB-1"}'
  if grep -qF 'issue(id:"SB-1")' "$GQL_LOG"; then ok "pack: one GraphQL query for SB-1 went to the stub"
  else bad "pack: the linear-gql.sh stub never saw a query for SB-1"; fi

  printf 'changed\n' >>"$prepo/README.md"
  if pack="$(cd "$prepo" && bash "$LINEAR_SH" pack SB-1 2>"$WORK/stderr")"; then
    pack_field 'git.dirty is true after editing a tracked file' '.git.dirty == true'
  else
    bad "pack (dirty tree) failed: $(cat "$WORK/stderr")"
  fi
else
  bad "pack failed: $(cat "$WORK/stderr")"
fi

# NEGATIVE: a bad key must be refused before any call is made.
if (cd "$prepo" && bash "$LINEAR_SH" pack not-a-key >/dev/null 2>"$WORK/stderr"); then
  bad "pack: accepted 'not-a-key'"
elif grep -q 'usage: pack SB-123' "$WORK/stderr"; then ok "pack: rejects a malformed key with usage"
else bad "pack: 'not-a-key' failed without the usage message: $(cat "$WORK/stderr")"; fi

# ----------------------------------------------- 4-6. python unit tests

note "4-6. python unittest in $tests_dir"
if (cd "$tests_dir" && SKILLS_DIR="$SKILLS" PYTHONDONTWRITEBYTECODE=1 \
      python3 -m unittest discover -s . -p 'test_*.py' -v >"$WORK/py.out" 2>&1); then
  ok "python: $(grep -E '^Ran [0-9]+ tests?' "$WORK/py.out" || echo 'ran (count not reported)')"
else
  bad "python unit tests failed:"
  sed 's/^/    | /' "$WORK/py.out" >&2
fi
py_ran="$(sed -nE 's/^Ran ([0-9]+) tests?.*/\1/p' "$WORK/py.out")"
[ "${py_ran:-0}" -ge 15 ] || bad "python: expected at least 15 tests to run, unittest reported '${py_ran:-none}' — discovery broken?"

# ---------------------------------------------------------------- verdict

if [ -e "$NET_MARKER" ]; then
  bad "a network stub was called — these tests must be offline: $(tr '\n' ';' <"$NET_MARKER")"
fi
[ "$fails" -eq 0 ] || die "check-linear-crud: $fails failure(s)"
note "check-linear-crud: all offline tests passed"
