#!/usr/bin/env bash
# Prepare the runner's $HOME inside the pod. Runs as the CronJob's
# initContainer, on every tick, and is idempotent.
#
# Three jobs, in order:
#
#   1. sync the skills, commands and agents from dotfiles@main
#   2. give git an identity and a credential helper
#   3. make sure every repo in repos.json has a primary clone to cut from
#
# WHY THE SKILLS ARE NOT BAKED INTO THE IMAGE. The image is the toolchain and
# changes weekly at most (SB-978 bumps it behind a green suite). The skills
# change several times a day — they are what the runner is delivering. Baking
# them in would mean an image rebuild to ship a one-line fix to run.sh, and a
# tick running whatever was current when the image was last built. Cloning at
# tick time means the runner always runs the merged code, which is exactly the
# property the loop depends on.
#
# The trade is that a bad merge to main reaches the next tick with no gate. It
# already did under launchd — chezmoi apply on the mini had the same shape —
# so this is not a new exposure.
set -euo pipefail

: "${HOME:?bootstrap needs HOME set to the persistent volume}"
DOTFILES_REPO="${DOTFILES_REPO:-https://github.com/silverbeer/dotfiles}"
DOTFILES_REF="${DOTFILES_REF:-main}"
GIT_USER_NAME="${GIT_USER_NAME:-silverbeer}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-silverbeer.io@gmail.com}"

note() { printf 'bootstrap: %s\n' "$*"; }

# ------------------------------------------------------------ 1. the skills

# The initContainer clones dotfiles in order to run THIS script, so it passes
# that clone in rather than making us fetch the same commit twice. Cloning here
# too is the fallback for running it by hand.
if [ -n "${DOTFILES_SRC:-}" ] && [ -d "${DOTFILES_SRC}/.git" ]; then
  checkout="$DOTFILES_SRC"
else
  src="$(mktemp -d)"
  trap 'rm -rf "$src"' EXIT
  git clone --quiet --depth 1 --branch "$DOTFILES_REF" "$DOTFILES_REPO" "$src/dotfiles"
  checkout="$src/dotfiles"
fi
note "dotfiles@$DOTFILES_REF $(git -C "$checkout" rev-parse --short HEAD)"

# chezmoi's only renaming here is the top-level `dot_claude` -> `.claude`;
# nothing nested carries a dot_/private_/executable_ prefix except
# modify_settings.json.tmpl and statusLine.sh, neither of which a headless tick
# uses. Asserted rather than assumed — a new prefixed file appearing under
# dot_claude would otherwise be copied under its source name and silently not
# be the file the runner looks for.
if find "$checkout/dot_claude" -mindepth 2 \
     \( -name 'dot_*' -o -name 'private_*' -o -name 'symlink_*' \) | grep -q .; then
  echo "bootstrap: a nested chezmoi-prefixed path appeared under dot_claude —" >&2
  echo "  this copy does not rename those, so it would land under the wrong name." >&2
  find "$checkout/dot_claude" -mindepth 2 \
    \( -name 'dot_*' -o -name 'private_*' -o -name 'symlink_*' \) >&2
  exit 1
fi

mkdir -p "$HOME/.claude"
for d in skills commands agents; do
  rm -rf "$HOME/.claude/$d"
  cp -R "$checkout/dot_claude/$d" "$HOME/.claude/$d"
done
cp "$checkout/dot_claude/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
note "synced .claude/{skills,commands,agents} and CLAUDE.md"

# ------------------------------------------------------------------ 2. git

git config --global user.name "$GIT_USER_NAME"
git config --global user.email "$GIT_USER_EMAIL"
git config --global init.defaultBranch main
# The worktrees live under $HOME/.local/state/cycle-runner/worktrees and the
# clones under $HOME/gitrepos; both are ours, but the pod's uid may not match
# whatever last wrote the volume.
git config --global --add safe.directory '*'

if [ -n "${GH_TOKEN:-}" ]; then
  # `gh auth setup-git` writes a credential helper that shells back to gh,
  # which reads GH_TOKEN. Nothing is written to disk in plaintext.
  gh auth setup-git
  note "git credential helper wired to gh (GH_TOKEN present)"
else
  note "GH_TOKEN is not set — pushes will fail; check the Secret"
fi

# --------------------------------------------------------------- 3. clones

# `repo_dir_for_label` resolves $HOME/gitrepos/<dirGlob>, and a worktree is cut
# from whatever it finds. No clone, no worktree, no run — and the error surfaces
# deep inside /work-headless rather than here.
#
# Blobless (--filter=blob:none): full history for branch/merge-base work, blobs
# fetched on demand. Fourteen repos this way is minutes on first tick and free
# afterwards, because the volume persists.
repos_json="$HOME/.claude/skills/linear-crud/repos.json"
[ -r "$repos_json" ] || { echo "bootstrap: no repos.json at $repos_json" >&2; exit 1; }

mkdir -p "$HOME/gitrepos"
cloned=0 present=0 failed=0
while read -r gh_repo; do
  [ -z "$gh_repo" ] && continue
  dest="$HOME/gitrepos/$gh_repo"
  if [ -d "$dest/.git" ]; then
    present=$((present + 1))
    continue
  fi
  if git clone --quiet --filter=blob:none \
      "https://github.com/silverbeer/$gh_repo" "$dest" 2>/dev/null; then
    cloned=$((cloned + 1))
  else
    # A repo that cannot be cloned is not fatal: the runner only needs the one
    # its ticket is labelled for, and failing the whole tick because an
    # unrelated repo was renamed would be worse than the error /work-headless
    # gives when it actually needs it.
    note "could not clone $gh_repo — tickets labelled for it will fail"
    rm -rf "$dest"
    failed=$((failed + 1))
  fi
done < <(jq -r '.[].ghRepo // empty' "$repos_json" | sort -u)

note "clones: $present present, $cloned new, $failed unavailable"
