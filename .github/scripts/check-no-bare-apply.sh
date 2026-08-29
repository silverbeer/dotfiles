#!/usr/bin/env bash
# The single most dangerous edit anyone can make to this repo's CI is dropping
# `--dry-run` from a chezmoi apply. A bare apply on a runner executes the run_*
# scripts: run_once_after_20-linear-api-key.sh calls `op read`, and
# run_onchange_install-brew-tools.sh.tmpl calls `brew install`.
#
# The dry-run checks themselves cannot catch this — with the flag removed they
# would happily "pass" while doing real work. So this is a static check over the
# workflows, the check scripts and the test suite. It needs no binaries at all.
#
# WHAT IT DOES NOT CATCH, stated plainly so nobody trusts it further than it
# goes: an apply hidden inside a single-quoted string is treated as data, not as
# a call site (that is what makes .github/tests/ scannable at all — those files
# quote bare applies on purpose, as fixtures). An apply assembled from variables,
# or one produced by `eval`, is invisible to any static check of this kind.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

rc=0
found=0

# chezmoi (or the cm() wrapper) invoking apply. The trailing class is any
# non-word character, not just whitespace: `cm apply;` and `cm apply)` are call
# sites too, and `([[:space:]]|$)` let both through.
pattern='(^|[^-[:alnum:]_])(chezmoi|cm)[[:space:]][^&|;]*apply([^-[:alnum:]_]|$)'

# Emit "<line number>:<logical line>", joining backslash continuations onto the
# line they start on. Without this a call site split as
#
#     cm --source "$PWD" \
#       apply -v
#
# matched nothing at all: the regex is single-line, and neither half of that is
# a chezmoi apply on its own.
logical_lines() {
  awk '
    { if (cont) { acc = acc " " $0 } else { acc = $0; start = NR } }
    acc ~ /\\$/ { sub(/\\$/, "", acc); cont = 1; next }
    { cont = 0; print start ":" acc; acc = "" }
    END { if (cont) print start ":" acc }
  ' "$1"
}

scan() {
  _f="$1"
  [ -f "$_f" ] || return 0
  _rel="${_f#"$REPO"/}"
  while IFS= read -r _line; do
    [ -n "$_line" ] || continue
    _n="${_line%%:*}"
    _code="${_line#*:}"

    # Reduce the line to the part that would actually RUN: strip leading
    # whitespace, blank out single-quoted literals, drop a trailing comment.
    _code="$(printf '%s' "$_code" | sed -e 's/^[[:space:]]*//' -e "s/'[^']*'/''/g")"
    case "$_code" in '#'*) continue ;; esac
    _code="${_code%%#*}"

    # The prefilter matched the raw line; re-match what is left, or a mention
    # inside a comment or a fixture string would be reported as a call site.
    # This is also what closes the inline-comment bypass: the old check tested
    # `*--dry-run*` against the WHOLE line, so
    #   cm apply -v  # TODO put --dry-run back
    # classified as compliant.
    printf '%s' "$_code" | grep -qE "$pattern" || continue

    found=$((found + 1))
    case "$_code" in
      *--dry-run*) note "ok   $_rel:$_n" ;;
      *)
        err_file "$_rel" "line $_n: chezmoi apply without --dry-run"
        rc=1
        ;;
    esac
  done <<EOF
$(logical_lines "$_f" | grep -E "$pattern" || true)
EOF
}

# Every workflow, not just ci.yml by name: a second workflow file is exactly
# where an unguarded apply would land unnoticed.
for w in "$REPO"/.github/workflows/*.yml "$REPO"/.github/workflows/*.yaml; do
  scan "$w"
done

scan "$REPO/.github/scripts/lib.sh"
for s in "$REPO"/.github/scripts/check-*.sh; do
  [ -f "$s" ] || continue
  # Skip this file. Its own error and report strings contain the words
  # "chezmoi apply ... --dry-run", so scanning itself counted two phantom call
  # sites — which would hold `found` above zero and silently disarm the
  # "stopped matching anything" tripwire below even after every real call site
  # had gone.
  [ "$s" = "$REPO/.github/scripts/check-no-bare-apply.sh" ] && continue
  scan "$s"
done

# The test suite runs on the runner and on any laptop following README's "no
# network, no installs" promise, so an apply here is as dangerous as one in the
# workflow. test_ci_config_in_home.sh already invokes chezmoi outside cm();
# changing that call to `apply` used to be invisible.
for t in "$REPO"/.github/tests/*.sh; do
  scan "$t"
done

# If the pattern stops matching anything, the check has become decoration and
# a bare apply could be reintroduced with nothing to notice.
if [ "$found" -eq 0 ]; then
  die "no 'chezmoi apply' call found anywhere — this check no longer guards anything"
fi

if [ "$rc" -eq 0 ]; then
  note "all $found chezmoi apply call(s) carry --dry-run"
fi
exit "$rc"
