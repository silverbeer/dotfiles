#!/usr/bin/env bash
# shellcheck disable=SC2016  # fixtures are literal shell/jq source; expansion
#                             is exactly what must NOT happen here.
# check-shellcheck.sh and check-templates-render.sh.
#
# Two shell scripts in this repo are invisible to a `*.sh` glob and are the
# most dangerous ones in it:
#
#   dot_claude/modify_settings.json.tmpl        named .json, but bash
#   run_onchange_install-brew-tools.sh.tmpl     bash wrapped in Go template
#
# Both run on every `chezmoi apply`. A syntax error in the first corrupts
# ~/.claude/settings.json on every machine.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=harness.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/harness.sh"

# ------------------------------------------------------------- shellcheck

test_all_tracked_shell_is_clean() {
  need_bin shellcheck git
  assert_ok check-shellcheck.sh
  assert_out 'tracked *.sh'
  assert_out 'the 2 shell scripts the *.sh glob misses'
}

# NEGATIVE: the explicit-files mode must actually run shellcheck. ci.yml's
# chezmoi job uses that mode on the RENDERED template, so a mode that silently
# accepted everything would take a whole job's worth of checking with it.
test_named_files_mode_rejects_a_bad_script() {
  need_bin shellcheck
  work="$(new_dir)"
  printf '#!/usr/bin/env bash\nx=1\nif [ $x == 1 ]; then echo "$undefined_var"; fi\n' \
    >"$work/bad.sh"

  assert_fail check-shellcheck.sh "$work/bad.sh"
  assert_out 'SC'
}

test_named_files_mode_accepts_a_clean_script() {
  need_bin shellcheck
  work="$(new_dir)"
  printf '#!/usr/bin/env bash\nset -euo pipefail\nx=1\necho "$x"\n' >"$work/ok.sh"

  assert_ok check-shellcheck.sh "$work/ok.sh"
  assert_out 'checking 1 explicitly named file'
}

# NEGATIVE: the modify_ script is checked despite its .json.tmpl name. Break it
# and the check must notice — a *.sh glob never would.
test_a_broken_modify_settings_template_is_rejected() {
  need_bin shellcheck git
  src="$(copy_source)"
  printf 'if [ -z "$x" ; then echo oops; fi\n' \
    >>"$src/dot_claude/modify_settings.json.tmpl"

  export REPO="$src"
  assert_fail check-shellcheck.sh
  # Named, because a bare assert_fail here passed on ANY non-zero exit — a stray
  # untracked file in the working tree used to get copied into the fixture and
  # fail the check first, so this test reported PASS with no breakage injected.
  assert_out 'dot_claude/modify_settings.json.tmpl'
  assert_out 'SC1073'
}

# NEGATIVE: same for the brew installer, which is only reachable after the
# whole-line {{ ... }} directives are deleted.
test_a_broken_brew_template_is_rejected() {
  need_bin shellcheck git
  src="$(copy_source)"
  # Insert inside the {{ if }} guard, so the breakage only exists after
  # de-templating — which is the whole point of that step.
  tmpl="$src/run_onchange_install-brew-tools.sh.tmpl"
  awk '/^\{\{ end -\}\}/ && !d { print "if [ -z \"$x\" ; then echo oops; fi"; d = 1 } { print }' \
    "$tmpl" >"$tmpl.new"
  mv "$tmpl.new" "$tmpl"
  grep -q 'echo oops' "$tmpl" || fail "fixture did not inject a syntax error"

  export REPO="$src"
  assert_fail check-shellcheck.sh
  # 'In - line' is shellcheck reading the DE-TEMPLATED script on stdin, which is
  # the only pass that can see this breakage — so this pins the right pass, not
  # merely a non-zero exit.
  assert_out 'In - line'
  assert_out 'SC1073'
}

# copy_source must hand a fixture the tracked tree and nothing else. When it was
# a `cp -R` of the working directory, an untracked scratch file came along, got
# `git add -A`ed, and failed the check under test before the injected breakage
# was ever reached — which is what the two assert_out calls above now catch, and
# what this catches at the source.
test_copy_source_contains_only_tracked_files() {
  need_bin git
  work="$(new_dir)"
  src="$(copy_source)"
  ( cd "$src" && find . -type f -not -path './.git/*' | sed 's|^\./||' | sort ) \
    >"$work/copied.txt"
  ( cd "$REPO_ROOT" && git ls-files | sort ) >"$work/tracked.txt"
  diff -u "$work/tracked.txt" "$work/copied.txt" >"$work/diff.txt" \
    || fail "copy_source did not reproduce the tracked file set exactly:
$(cat "$work/diff.txt")"
}

# The de-templating must DELETE the {{ ... }} lines, not blank them: blanking
# moves the shebang off line 1 and yields a spurious SC1128, which would make
# the check fail for a reason that has nothing to do with the script.
test_detemplating_keeps_the_shebang_on_line_one() {
  first="$(sed -E '/^\{\{.*\}\}[[:space:]]*$/d' \
    "$REPO_ROOT/run_onchange_install-brew-tools.sh.tmpl" | sed -n 1p)"
  case "$first" in
    '#!'*) ;;
    *) fail "de-templated line 1 is '$first', not a shebang" ;;
  esac
}

# --------------------------------------------------------------- templates

test_all_templates_render() {
  need_bin chezmoi
  assert_ok check-templates-render.sh
  assert_out 'dot_claude/modify_settings.json.tmpl ->'
  assert_out 'dot_zshrc.tmpl ->'
  assert_out 'run_onchange_install-brew-tools.sh.tmpl ->'
}

# NEGATIVE: a template that no longer parses.
test_an_unparseable_template_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  printf '{{ this is not valid go template\n' >>"$src/dot_zshrc.tmpl"

  export REPO="$src"
  assert_fail check-templates-render.sh
  assert_out 'dot_zshrc.tmpl failed to render'
}

# NEGATIVE: a template that renders to nothing. chezmoi reports success and
# deploys an empty ~/.zshrc, which is a far worse outcome than a parse error
# and is exactly what a mis-scoped {{ if }} produces.
test_a_template_rendering_to_zero_bytes_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  # No trailing newline: a newline after {{ end }} is emitted, and the point
  # here is a render of exactly 0 bytes.
  printf '{{ if false }}whole file{{ end }}' >"$src/dot_zshrc.tmpl"

  export REPO="$src"
  assert_fail check-templates-render.sh
  assert_out 'dot_zshrc.tmpl rendered to 0 bytes'
}

# ...but an empty render of the brew installer is CORRECT on Linux, where the
# {{ if eq .chezmoi.os "darwin" }} guard evaluates false. The check must not
# fail the ubuntu leg of the matrix.
test_an_empty_brew_render_is_allowed() {
  need_bin chezmoi
  src="$(copy_source)"
  sed 's|{{ if eq .chezmoi.os "darwin" -}}|{{ if eq .chezmoi.os "plan9" -}}|' \
    "$src/run_onchange_install-brew-tools.sh.tmpl" >"$src/t.new"
  mv "$src/t.new" "$src/run_onchange_install-brew-tools.sh.tmpl"

  export REPO="$src"
  assert_ok check-templates-render.sh
  assert_out 'run_onchange_install-brew-tools.sh.tmpl -> 0 bytes'
}

# NEGATIVE: a template deleted from the source but still listed.
test_a_missing_template_is_rejected() {
  need_bin chezmoi
  src="$(copy_source)"
  rm -f "$src/dot_zshrc.tmpl"

  export REPO="$src"
  assert_fail check-templates-render.sh
  assert_out 'template dot_zshrc.tmpl is missing'
}

run_tests
