#!/usr/bin/env bash
# shellcheck shell=bash
#
# Shared setup for the check-*.sh scripts in this directory. Sourced, never run.
#
# Each check is a standalone script so that ci.yml and .github/tests/ run the
# SAME code. If you find yourself copying a check into a `run:` block, stop —
# the tests would then be exercising a fork of the logic, which is worse than
# having no tests at all.
#
# Knobs (all optional, all overridden by .github/tests/):
#   REPO  chezmoi source dir under test          default: this repository
#   WORK  scratch dir for intermediate artefacts default: a fresh mktemp -d
#   DEST  chezmoi destination, and $HOME         default: $WORK/home
#
# Every chezmoi call goes through cm(), which pins HOME *and the three XDG base
# dirs* to $DEST. That is what makes these safe to run on a laptop: chezmoi
# never sees the real HOME, so it can neither read the machine's chezmoi.toml
# (which would make results depend on local config) nor write its cache or
# persistent state there.
#
# HOME alone was not enough. chezmoi resolves its config as $XDG_CONFIG_HOME/
# chezmoi/chezmoi.toml and only falls back to $HOME/.config when XDG_CONFIG_HOME
# is unset — so on any machine exporting it, which is routine on Linux, the
# machine's own chezmoi.toml was still being read. Measured on chezmoi v2.70.0:
# a `[data]` key in a foreign XDG config rendered into these checks' output.

if [ -z "${REPO:-}" ]; then
  REPO="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
fi

if [ -z "${WORK:-}" ]; then
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/dotfiles-ci.XXXXXX")"
  # Only clean up what we created. A caller-supplied WORK belongs to the caller.
  trap 'rm -rf "$WORK"' EXIT
fi

: "${DEST:="$WORK/home"}"
mkdir -p "$WORK" "$DEST"

# The three templates that chezmoi has to render. Kept here so the render check
# and the shellcheck-the-rendered-output check cannot drift apart.
# shellcheck disable=SC2034  # consumed by check-templates-render.sh
TEMPLATES=(
  dot_claude/modify_settings.json.tmpl
  dot_zshrc.tmpl
  run_onchange_install-brew-tools.sh.tmpl
)

# The XDG values are exactly what chezmoi would derive from HOME="$DEST" if the
# caller's environment were empty, so pinning them changes nothing except that
# an inherited value can no longer win.
cm() {
  HOME="$DEST" \
  XDG_CONFIG_HOME="$DEST/.config" \
  XDG_CACHE_HOME="$DEST/.cache" \
  XDG_DATA_HOME="$DEST/.local/share" \
    chezmoi --source "$REPO" --destination "$DEST" --no-tty "$@"
}

# GitHub annotations when running in Actions, plain text otherwise. Either way
# the message goes to stderr so it survives a caller capturing stdout.
err() { printf '::error::%s\n' "$*" >&2; }
err_file() { f="$1"; shift; printf '::error file=%s::%s\n' "$f" "$*" >&2; }
note() { printf '  %s\n' "$*"; }
die() { err "$*"; exit 1; }

# --- assertions, factored out so .github/tests/ can exercise them directly ---

# A chezmoi `apply --dry-run` must render the run_* scripts without executing
# them and without writing anything. Both matter: run_once_after_20 calls
# `op read` and run_onchange_install-brew-tools calls `brew install`.
#
# A missing directory is a FAILURE, not an empty one: `find` on a path that does
# not exist writes to stderr and prints nothing, so the count came back 0 and
# this — the assertion that proves the dry run wrote nothing — passed against a
# $DEST that was never created.
assert_dir_empty() {
  _adr_dir="$1"; _adr_msg="$2"
  if [ ! -d "$_adr_dir" ]; then
    err "$_adr_msg (no such directory: $_adr_dir)"
    return 1
  fi
  _adr_n="$(find "$_adr_dir" -mindepth 1 | wc -l | tr -d ' ')"
  if [ "$_adr_n" != "0" ]; then
    err "$_adr_msg (found $_adr_n path(s))"
    find "$_adr_dir" -mindepth 1 | sed "s|^$_adr_dir|<dest>|" >&2
    return 1
  fi
  return 0
}

assert_unchanged() {
  _au_before="$1"; _au_after="$2"; _au_msg="$3"
  if ! cmp -s "$_au_before" "$_au_after"; then
    err "$_au_msg"
    return 1
  fi
  return 0
}
