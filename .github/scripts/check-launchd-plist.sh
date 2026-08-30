#!/usr/bin/env bash
# Offline tests for Library/LaunchAgents/io.silverbeer.cycle-runner.plist.tmpl
# (SB-930): it must render a valid plist ONLY when `.chezmoi.hostname` is the
# mac mini's short hostname, and render to nothing at all — no file, not an
# empty one — everywhere else.
#
# `.chezmoi.hostname` cannot be faked with the HOSTNAME environment variable
# (measured: chezmoi calls the OS hostname syscall directly, HOSTNAME=x in the
# environment changes nothing). It CAN be faked with a scratch chezmoi.toml
# carrying `[data.chezmoi]\nhostname = "x"` under a scratch XDG_CONFIG_HOME —
# measured against this repo's actual chezmoi.toml search path (also honoured
# by the plain `cm()` wrapper check-apply-dry-run.sh already uses, which pins
# HOME and the XDG dirs the same way). That is the mechanism this file uses;
# nothing here touches the real machine's chezmoi config.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

command -v jq >/dev/null 2>&1 || die "jq is not installed"

TMPL="$REPO/Library/LaunchAgents/io.silverbeer.cycle-runner.plist.tmpl"
[ -f "$TMPL" ] || die "missing $TMPL — wrong REPO?"

MINI_HOST="Toms-Mac-mini"
OTHER_HOST="some-other-mac"

# A scratch chezmoi.toml overriding .chezmoi.hostname, then a `chezmoi` call
# pinned to $DEST/the scratch config the same way lib.sh's cm() pins HOME —
# so this never reads or writes the real machine's chezmoi state.
cm_host() {  # HOSTNAME -- ARGS...
  _ch_host="$1"; shift
  _ch_dest="$WORK/dest-$_ch_host"
  _ch_cfg="$WORK/cfg-$_ch_host"
  mkdir -p "$_ch_dest" "$_ch_cfg/chezmoi"
  printf '[data.chezmoi]\n    hostname = "%s"\n' "$_ch_host" >"$_ch_cfg/chezmoi/chezmoi.toml"
  HOME="$_ch_dest" \
  XDG_CONFIG_HOME="$_ch_cfg" \
  XDG_CACHE_HOME="$WORK/cache-$_ch_host" \
  XDG_DATA_HOME="$WORK/data-$_ch_host" \
    chezmoi --source "$REPO" --destination "$_ch_dest" --no-tty "$@"
}

rc=0

# ------------------------------------------------ 1. mini: valid, non-empty

note "1. render for hostname=$MINI_HOST"
mini_out="$WORK/mini.plist"
if ! cm_host "$MINI_HOST" execute-template -f "$TMPL" >"$mini_out"; then
  err "template failed to render for hostname=$MINI_HOST"
  rc=1
fi
mini_bytes="$(wc -c <"$mini_out" | tr -d ' ')"
if [ "$mini_bytes" -eq 0 ]; then
  err "hostname=$MINI_HOST rendered to 0 bytes — the mini must get a real plist"
  rc=1
else
  note "hostname=$MINI_HOST -> $mini_bytes bytes"
fi

if command -v plutil >/dev/null 2>&1; then
  if plutil -lint "$mini_out" >"$WORK/plutil.out" 2>&1; then
    note "plutil -lint: OK"
  else
    err "plutil -lint rejected the mini-hostname render:"
    sed 's/^/    | /' "$WORK/plutil.out" >&2
    rc=1
  fi
else
  note "plutil not on PATH (non-macOS runner) — skipping the plist syntax lint, byte-count and content checks below still ran"
fi

for want in 'io.silverbeer.cycle-runner' '<integer>1800</integer>' '<false/>' \
            '.local/state/cycle-runner'; do
  grep -qF -- "$want" "$mini_out" || { err "mini-hostname render is missing '$want'"; rc=1; }
done

# ------------------------------------------------ 2. non-mini: nothing at all

note "2. render for hostname=$OTHER_HOST"
other_out="$WORK/other.plist"
if ! cm_host "$OTHER_HOST" execute-template -f "$TMPL" >"$other_out"; then
  err "template failed to render for hostname=$OTHER_HOST (it must render to empty, not error)"
  rc=1
fi
other_bytes="$(wc -c <"$other_out" | tr -d ' ')"
if [ "$other_bytes" -ne 0 ]; then
  err "hostname=$OTHER_HOST rendered $other_bytes byte(s) — every other Mac must get nothing"
  rc=1
else
  note "hostname=$OTHER_HOST -> 0 bytes (correct)"
fi

# --------------------------------------- 3. full apply --dry-run, both ways
#
# The empty-string render above proves the TEMPLATE is right; this proves
# chezmoi's own "empty content -> no target" behaviour actually holds for a
# full apply (measured, not assumed — see the file header).

note "3. apply --dry-run: hostname=$OTHER_HOST must create nothing under Library/"
if ! cm_host "$OTHER_HOST" apply --dry-run -v >"$WORK/apply-other.log" 2>&1; then
  err "apply --dry-run failed for hostname=$OTHER_HOST"
  sed -n '1,40p' "$WORK/apply-other.log" >&2
  rc=1
fi
if [ -e "$WORK/dest-$OTHER_HOST/Library/LaunchAgents/io.silverbeer.cycle-runner.plist" ]; then
  err "hostname=$OTHER_HOST: the plist exists on disk after apply --dry-run — it must not"
  rc=1
else
  note "hostname=$OTHER_HOST: no plist on disk (correct)"
fi

note "4. apply --dry-run: hostname=$MINI_HOST must offer the plist in the diff"
if ! cm_host "$MINI_HOST" apply --dry-run -v >"$WORK/apply-mini.log" 2>&1; then
  err "apply --dry-run failed for hostname=$MINI_HOST"
  sed -n '1,40p' "$WORK/apply-mini.log" >&2
  rc=1
fi
if ! grep -q 'Library/LaunchAgents/io.silverbeer.cycle-runner.plist' "$WORK/apply-mini.log"; then
  err "hostname=$MINI_HOST: apply --dry-run's diff never mentions the plist"
  sed -n '1,40p' "$WORK/apply-mini.log" >&2
  rc=1
else
  note "hostname=$MINI_HOST: apply --dry-run offers to create the plist (correct)"
fi

[ "$rc" -eq 0 ] && note "check-launchd-plist: all checks passed"
exit "$rc"
