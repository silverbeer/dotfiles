#!/usr/bin/env bash
# check-gitleaks.sh and the allowlists in .gitleaks.toml.
#
# The allowlists exist because this repo is FULL of things that look like
# secrets and are not: `op://agents/...` references, the path to the service
# account token file, the line in private_dot_zshenv that reads the Linear key
# out of a file. Allowlisting is by REGEX and never by path — a path allowlist
# on SETUP.md would blind the scanner to a real key pasted into the docs, which
# is this repo's single most likely mistake.
#
# So there are two things to prove, and the second is worthless without the
# first: that gitleaks still DETECTS, and that the allowlist suppresses only
# what it is meant to.
#
# ---------------------------------------------------------------------------
# The fixture key is assembled from two halves at runtime and NEVER appears as
# a literal in this file. That is not decoration: a literal would be a live
# finding in this public repo's own gitleaks scan, and the usual fix — adding a
# path allowlist for .github/tests — is exactly the blindness the config's own
# header forbids. test_meta.sh asserts the literal is absent.
#
# The body is a made-up 16-character string. It is NOT AWS's published
# documentation example (AKIA + IOSFODNN7EXAMPLE): measured against gitleaks
# 8.30.0, the default ruleset does not flag that value at all — the built-in
# allowlist drops anything containing EXAMPLE. A suite built on it would have
# reported "gitleaks detects nothing" as a pass.
# ---------------------------------------------------------------------------
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

fake_key() {
  _p='AKIA'
  _b='ZQ3T7X2WNBVR5CDM'
  printf '%s%s' "$_p" "$_b"
}

# A git repo containing exactly one file, committed.
fixture_repo() {
  _d="$(new_dir)"
  printf '%s\n' "$2" >"$_d/$1"
  git -C "$_d" init -q
  git -C "$_d" add -A
  git -C "$_d" -c user.name=qe -c user.email=qe@example.invalid \
    commit -qm 'fixture' >/dev/null
  printf '%s' "$_d"
}

# A config with the default ruleset and none of this repo's allowlists, used to
# establish that gitleaks detects the fixture at all.
bare_config() {
  _c="$(new_dir)/bare.toml"
  printf '[extend]\nuseDefault = true\n' >"$_c"
  printf '%s' "$_c"
}

# --------------------------------------------------------------- positives

test_the_real_repo_is_clean() {
  need_bin gitleaks git
  assert_ok check-gitleaks.sh
}

# --------------------------------------------------------------- negatives

# NEGATIVE, and the foundation for everything below: a bare AWS key must be
# flagged. If this ever stops failing, the suppression test below becomes
# meaningless and every other gitleaks result in this file is noise.
test_a_bare_aws_key_is_flagged_by_the_default_rules() {
  need_bin gitleaks git
  repo="$(fixture_repo leak.txt "aws_access_key_id = $(fake_key)")"

  bare="$(bare_config)"
  export REPO="$repo" GITLEAKS_CONFIG="$bare"
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '
}

# NEGATIVE: the same key, scanned with THIS repo's config. The allowlists must
# not have widened to the point of hiding a real key.
test_a_bare_aws_key_is_flagged_by_this_repos_config() {
  need_bin gitleaks git
  repo="$(fixture_repo leak.txt "aws_access_key_id = $(fake_key)")"

  export REPO="$repo" GITLEAKS_CONFIG="$REPO_ROOT/.gitleaks.toml"
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '
}

# POSITIVE: the op:// allowlist. A 1Password reference is a pointer into a
# vault, never the secret, and this repo is full of them.
test_a_key_on_an_op_reference_line_is_suppressed() {
  need_bin gitleaks git
  repo="$(fixture_repo ref.txt \
    "key=\$(op read 'op://agents/aws/access-key-id')  # e.g. $(fake_key)")"

  export REPO="$repo" GITLEAKS_CONFIG="$REPO_ROOT/.gitleaks.toml"
  assert_ok check-gitleaks.sh
}

# NEGATIVE, and the one that matters most: the allowlist is regexTarget="line",
# so it must suppress ONLY the line carrying the op:// reference. A real key
# two lines below a legitimate op:// reference — which is precisely what a
# careless paste into SETUP.md looks like — must still be caught.
test_a_key_on_a_different_line_from_the_op_reference_is_still_flagged() {
  need_bin gitleaks git
  repo="$(fixture_repo mixed.txt \
"# see op://agents/aws/access-key-id for the real value
# pasted here while debugging:
aws_access_key_id = $(fake_key)")"

  export REPO="$repo" GITLEAKS_CONFIG="$REPO_ROOT/.gitleaks.toml"
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '
}

# The op:// allowlist must not be so loose that any line mentioning the scheme
# disarms the scanner. A bare 'op://' with no vault/item path is not a
# reference and must not suppress.
#
# The second fixture is the one with teeth. The first passed only because it
# carries no second slash: the vault segment used to admit spaces, so ANY later
# slash on the line made the prose in front of it parse as a vault name and the
# whole line went quiet. One character apart.
test_a_malformed_op_scheme_does_not_suppress() {
  need_bin gitleaks git
  export GITLEAKS_CONFIG="$REPO_ROOT/.gitleaks.toml"

  REPO="$(fixture_repo loose.txt "op:// $(fake_key)")"; export REPO
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '

  REPO="$(fixture_repo loose2.txt "op:// vault docs/step 2 $(fake_key)")"; export REPO
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '
}

# NEGATIVE: the op:// vault segment must not span prose. Reproduced against the
# pre-fix config — the vault segment admitted spaces, so ' docs in SETUP md'
# parsed as a vault name, 'step 2 for <key>' as an item, and the key on that
# line was suppressed.
test_an_op_scheme_spanning_prose_does_not_suppress() {
  need_bin gitleaks git
  repo="$(fixture_repo notes.md "# see op:// docs in SETUP md/step 2 for $(fake_key)")"

  export REPO="$repo" GITLEAKS_CONFIG="$REPO_ROOT/.gitleaks.toml"
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '
}

# NEGATIVE: the agent-token allowlist is regexTarget="line", so unanchored it
# suppressed every finding on any line MENTIONING the path. SETUP.md:72 already
# carries that path, which made it a ready-made hiding place for a real token in
# this public repo.
test_a_key_sharing_a_line_with_the_agent_token_path_is_still_flagged() {
  need_bin gitleaks git
  repo="$(fixture_repo setup.md \
    "aws_access_key_id = $(fake_key)   # token file is ~/.config/op/agent-token")"

  export REPO="$repo" GITLEAKS_CONFIG="$REPO_ROOT/.gitleaks.toml"
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '
}

# NEGATIVE: same defect on the LINEAR_API_KEY allowlist — a key appended to the
# allowlisted line was suppressed with it.
test_a_key_appended_to_the_linear_key_line_is_still_flagged() {
  need_bin gitleaks git
  repo="$(fixture_repo zshenv.txt \
    "[[ -r \$_linear_key ]] && export LINEAR_API_KEY=\"\$(<\$_linear_key)\"  # was $(fake_key)")"

  export REPO="$repo" GITLEAKS_CONFIG="$REPO_ROOT/.gitleaks.toml"
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '
}

# ...and the complement of those two: anchoring an allowlist end to end is one
# edit away from anchoring it to nothing, at which point it suppresses no real
# finding and the next false positive gets "fixed" by widening it again. Both
# anchored regexes must still match the private_dot_zshenv line they exist for.
#
# grep is ERE and gitleaks is RE2, so this pins the intent of the anchor rather
# than gitleaks' exact evaluation of it; the suppression behaviour itself is
# covered by test_a_key_on_an_op_reference_line_is_suppressed above.
test_the_anchored_allowlists_still_match_their_real_lines() {
  zshenv="$REPO_ROOT/private_dot_zshenv"
  [ -f "$zshenv" ] || fail "private_dot_zshenv is gone; these allowlists have no subject"

  token=0
  linear=0
  while IFS= read -r re; do
    [ -n "$re" ] || continue
    grep -qE -- "$re" "$zshenv" || continue
    case "$re" in
      *agent-token*) token=1 ;;
      *LINEAR_API_KEY*) linear=1 ;;
    esac
  done <<EOF
$(sed -n "s/^regexes = \['''\(.*\)'''\]\$/\1/p" "$REPO_ROOT/.gitleaks.toml")
EOF

  [ "$token" -eq 1 ] \
    || fail "the agent-token allowlist matches no line in private_dot_zshenv — it is decoration now"
  [ "$linear" -eq 1 ] \
    || fail "the LINEAR_API_KEY allowlist matches no line in private_dot_zshenv — it is decoration now"
}

# The history scan is not decoration: a key removed from the working tree but
# still reachable on a branch has to be found. --log-opts=--all is what does
# this, and dropping it would leave every positive above still passing.
test_a_key_only_in_history_is_flagged() {
  need_bin gitleaks git
  repo="$(fixture_repo leak.txt "aws_access_key_id = $(fake_key)")"
  rm -f "$repo/leak.txt"
  printf 'clean\n' >"$repo/ok.txt"
  git -C "$repo" add -A
  git -C "$repo" -c user.name=qe -c user.email=qe@example.invalid \
    commit -qm 'remove the key' >/dev/null
  grep -rq 'AKIA' "$repo" --exclude-dir=.git \
    && fail "fixture still has the key in the working tree"

  export REPO="$repo" GITLEAKS_CONFIG="$REPO_ROOT/.gitleaks.toml"
  assert_fail check-gitleaks.sh
  assert_out 'leaks found: '
}

run_tests
