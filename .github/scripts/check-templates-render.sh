#!/usr/bin/env bash
# Every template must render against a clean, config-less chezmoi. Renders land
# in $WORK/rendered/ for check-shellcheck.sh to pick up.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/lib.sh"

out_dir="$WORK/rendered"
mkdir -p "$out_dir"
cd "$REPO"

rc=0
for t in "${TEMPLATES[@]}"; do
  [ -f "$t" ] || { err "template $t is missing"; rc=1; continue; }
  out="$out_dir/$(basename "$t").rendered"
  if ! cm execute-template -f "$t" >"$out"; then
    err "$t failed to render"
    rc=1
    continue
  fi
  bytes="$(wc -c <"$out" | tr -d ' ')"
  note "$t -> $bytes bytes"

  # run_onchange_install-brew-tools.sh.tmpl is wrapped in an
  # `{{ if eq .chezmoi.os "darwin" }}` guard, so on Linux an empty render is
  # correct. For the other two, empty means the template silently evaluated to
  # nothing — which renders as a successful apply that deploys an empty file.
  case "$t" in
    run_onchange_install-brew-tools.sh.tmpl) ;;
    *)
      if [ "$bytes" -eq 0 ]; then
        err "$t rendered to 0 bytes"
        rc=1
      fi
      ;;
  esac
done

exit "$rc"
