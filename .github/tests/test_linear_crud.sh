#!/usr/bin/env bash
# check-linear-crud.sh — the offline functional tests for the linear-crud and
# backlog-groom scripts. The positive case proves the check passes on the
# current tree; every negative below breaks ONE thing in a copy of the tree and
# asserts the check fails naming it, so a check that stops looking at that
# thing is caught here rather than by the next bad repos.json edit.
# shellcheck disable=SC2016  # the sed/grep patterns quote linear.sh's $-names literally
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

LINEAR_SH=dot_claude/skills/linear-crud/scripts/linear.sh
BOARD_PY=dot_claude/skills/linear-crud/scripts/executable_board.py
API_PY=dot_claude/skills/linear-crud/scripts/linear_api.py
APPLY_PY=dot_claude/skills/backlog-groom/scripts/apply.py
REPOS_JSON=dot_claude/skills/linear-crud/repos.json
GQL=dot_claude/skills/linear-crud/scripts/executable_linear-gql.sh

# sed -i differs between BSD and GNU; write to a sibling and move.
edit() { sed "$1" "$2" >"$2.new" && mv "$2.new" "$2"; }

test_the_check_passes_on_the_current_tree() {
  need_bin jq python3 git
  src="$(copy_source)"
  export REPO="$src"
  assert_ok check-linear-crud.sh
  assert_out "ok   repo_label 'bet-collect' -> BETC"
  assert_out "ok   epic_repo 'Unknown Thing' -> no match (exit 1)"
  assert_out 'ok   stats: BETC 2'
  assert_out 'ok   pack: branchName is silverbeer/sb-1-<slug>'
  assert_out 'ok   python: Ran '
  assert_out 'check-linear-crud: all offline tests passed'
}

# The tests must not need a key or a network. Prove the check passes with a
# key set to a junk value AND a real-looking key file path that does not exist:
# the gql stub is what answers, not linear-gql.sh. The check's stubs drop a
# marker in $WORK if anything reached for the network; WORK is ours here so
# the marker's absence can be asserted directly, not just via the log.
test_the_check_is_offline() {
  need_bin jq python3 git
  src="$(copy_source)"
  export REPO="$src"
  export LINEAR_API_KEY=unset
  export LINEAR_KEY_FILE="$src/no-such-key-file"
  WORK="$(new_dir)"; export WORK
  assert_ok check-linear-crud.sh
  assert_not_out 'was called'
  [ ! -e "$WORK/network-was-called" ] || fail "a network stub was called: $(cat "$WORK/network-was-called")"
}

# NEGATIVE: corrupt repos.json. Every repo_label row would fail; the check
# must say WHY, naming the file, before the table starts.
test_corrupt_repos_json_fails_naming_the_file() {
  need_bin jq python3 git
  src="$(copy_source)"
  printf '{ not json\n' >"$src/$REPOS_JSON"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'repos.json could not be loaded'
}

# NEGATIVE: drop the BETC entry. bet-collect then falls through to BET's
# `bet-*` glob — exactly the ordering bug the table exists to catch.
test_removing_the_betc_entry_fails_the_table() {
  need_bin jq python3 git
  src="$(copy_source)"
  jq 'map(select(.label != "BETC"))' "$src/$REPOS_JSON" >"$src/$REPOS_JSON.new"
  mv "$src/$REPOS_JSON.new" "$src/$REPOS_JSON"
  grep -q BETC "$src/$REPOS_JSON" && fail "fixture still contains BETC"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out "repo_label 'bet-collect': expected BETC, got 'BET'"
  assert_out "epic_repo 'BET — Acquisition & Capture': expected BETC"
}

# NEGATIVE: reorder so a glob that also matches comes first. Same bug from the
# other direction: BET's bet-* moved above BETC.
test_reordering_repos_json_fails_the_table() {
  need_bin jq python3 git
  src="$(copy_source)"
  jq '[.[1], .[0]] + .[2:]' "$src/$REPOS_JSON" >"$src/$REPOS_JSON.new"
  mv "$src/$REPOS_JSON.new" "$src/$REPOS_JSON"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out "repo_label 'bet-collect': expected BETC, got 'BET'"
  assert_out "repo_label 'bet-capture': expected BETC, got 'BET'"
}

# NEGATIVE: the stats program stops counting repo labels.
test_broken_stats_jq_fails() {
  need_bin jq python3 git
  src="$(copy_source)"
  edit 's/\$repos|index(\$n)/false/' "$src/$LINEAR_SH"
  grep -q 'select(. as $n|false)' "$src/$LINEAR_SH" || fail "fixture did not break the by-repo selector"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out "stats: expected 'BET 3' in:"
  assert_out "stats: expected 'BETC 2' in:"
}

# NEGATIVE: the branch name convention drifts. Linear's git automation keys
# off `sb-<n>` in the branch, so this is the one string pack must never change.
test_branch_name_drift_fails_pack() {
  need_bin jq python3 git
  src="$(copy_source)"
  edit 's|echo "silverbeer/\$(printf|echo "silverbeerio/$(printf|' "$src/$LINEAR_SH"
  grep -q 'echo "silverbeerio/' "$src/$LINEAR_SH" || fail "fixture did not change branch_name_for"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'pack: branchName is silverbeer/sb-1-<slug> — expected'
}

# NEGATIVE: pack drops a key.
test_pack_missing_a_key_fails() {
  need_bin jq python3 git
  src="$(copy_source)"
  edit 's/git:{branch:\$cur, dirty:\$dirty}, pr:\$pr}/git:{branch:$cur, dirty:$dirty}}/' "$src/$LINEAR_SH"
  grep -q 'dirty:\$dirty}}' "$src/$LINEAR_SH" || fail "fixture did not drop the pr key"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'pack: keys are issue,branchName,repoLabel,git,pr — expected'
}

# NEGATIVE: longest_path returns the wrong route.
test_broken_longest_path_fails_the_python_tests() {
  need_bin jq python3 git
  src="$(copy_source)"
  edit 's/u = max(graph\[u\], key=depth)/u = min(graph[u], key=depth)/' "$src/$BOARD_PY"
  grep -q 'u = min(graph\[u\], key=depth)' "$src/$BOARD_PY" || fail "fixture did not break longest_path"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'python unit tests failed'
  assert_out 'FAIL: test_longest_branch_wins_over_a_short_one_listed_first'
}

# NEGATIVE: find_cycle goes blind.
test_broken_find_cycle_fails_the_python_tests() {
  need_bin jq python3 git
  src="$(copy_source)"
  edit 's/return found\[0\] if found else None/return None/' "$src/$BOARD_PY"
  grep -q 'return None$' "$src/$BOARD_PY" || fail "fixture did not break find_cycle"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'FAIL: test_two_node_cycle_is_found_and_closed'
}

# NEGATIVE: warn_if_capped becomes strictly-greater, so a full page is silent.
test_off_by_one_warn_if_capped_fails() {
  need_bin jq python3 git
  src="$(copy_source)"
  edit 's/if len(nodes) >= cap:/if len(nodes) > cap:/' "$src/$API_PY"
  grep -q 'if len(nodes) > cap:' "$src/$API_PY" || fail "fixture did not change warn_if_capped"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'FAIL: test_warns_at_the_cap'
}

# NEGATIVE: apply.py's plan() stops comparing, so every change is re-applied
# on every run — the exact non-idempotence the docstring promises against.
test_non_idempotent_apply_plan_fails() {
  need_bin jq python3 git
  src="$(copy_source)"
  edit 's/if "priority" in c and cur\["priority"\] != c\["priority"\]:/if "priority" in c:/' "$src/$APPLY_PY"
  grep -q 'if "priority" in c:$' "$src/$APPLY_PY" || fail "fixture did not break plan()"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'FAIL: test_second_run_after_apply_plans_nothing'
}

# NEGATIVE: the python tests vanish. Discovery finding nothing must not be a
# green run.
test_missing_python_tests_fail() {
  need_bin jq python3 git
  src="$(copy_source)"
  rm "$src"/.github/tests/linear_crud/test_*.py
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'expected at least 15 tests to run'
}

# NEGATIVE: --http1.1 is dropped from linear-gql.sh. Every Linear call the
# cycle-runner makes goes through that curl, and on the image's curl 7.88 an
# authenticated POST over HTTP/2 dies with PROTOCOL_ERROR — reproduced
# in-cluster, 3/3 retries, twice, so the retry loop does not save it.
#
# The flag looks removable because an unauthenticated one-line query DOES
# succeed over h2. That is the trap this test exists for.
test_dropping_http11_from_linear_gql_is_rejected() {
  need_bin python3 jq
  src="$(copy_source)"
  edit 's/curl -sS --http1\.1/curl -sS/' "$src/$GQL"
  grep -q 'curl -sS --http1.1' "$src/$GQL" && fail "fixture did not drop the flag"
  export REPO="$src"
  assert_fail check-linear-crud.sh
  assert_out 'PROTOCOL_ERROR'
}

run_tests
