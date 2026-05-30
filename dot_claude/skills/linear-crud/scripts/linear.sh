#!/usr/bin/env bash
# linear-crud skill helper — thin wrapper around the `linear` CLI that bakes in
# the silverbeer workspace conventions so Claude never has to remember them.
#
# Conventions enforced:
#   - team    = SB (override: LINEAR_TEAM)
#   - assignee= silverbeer.io, ALWAYS (override: LINEAR_ASSIGNEE)
#   - repo label (group) detected from the current git repo
#   - type label (group) required on create
#
# Subcommands:
#   repo-label                 print the repo label inferred from cwd (MT/MS/MSA/QB/STK)
#   new   --title T (--body B | --body-file F) --type TYPE [--repo R] [--label L ...]
#   list  [--all] [--repo R]   my issues (open by default), scoped to current repo
#   move  SB-N "State"         change workflow state
#   link  SB-N [PR#]           add "Fixes SB-N" to a PR body (current branch's PR if # omitted)
#   audit-unassigned [--fix]   list unassigned SB issues; --fix assigns them to the default assignee
set -euo pipefail

LINEAR_TEAM="${LINEAR_TEAM:-SB}"
LINEAR_ASSIGNEE="${LINEAR_ASSIGNEE:-silverbeer.io}"

die() { echo "error: $*" >&2; exit 1; }

# Map the current git repo (or cwd) to its Linear `repo` label.
repo_label() {
  local root name
  root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  name="$(basename "$root")"
  case "$name" in
    missing-table)             echo "MT" ;;
    match-scraper)             echo "MS" ;;
    match-scraper-agent)       echo "MSA" ;;
    qualityplaybook*|qb)       echo "QB" ;;
    myrunstreak*|runstreak*)   echo "STK" ;;
    *) return 1 ;;
  esac
}

cmd_repo_label() { repo_label || die "unknown repo '$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")' — pass --repo explicitly"; }

cmd_new() {
  local title="" body="" body_file="" type="" repo="" extra_labels=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --title)     title="$2"; shift 2 ;;
      --body)      body="$2"; shift 2 ;;
      --body-file) body_file="$2"; shift 2 ;;
      --type)      type="$2"; shift 2 ;;
      --repo)      repo="$2"; shift 2 ;;
      --label)     extra_labels+=("$2"); shift 2 ;;
      *) die "new: unknown arg '$1'" ;;
    esac
  done
  [[ -n "$title" ]] || die "new: --title required"
  [[ -n "$type"  ]] || die "new: --type required (bug|feature|chore|docs|infra|security)"
  [[ -z "$repo" ]] && repo="$(repo_label || die "new: could not detect repo — pass --repo")"

  local args=(issue create --team "$LINEAR_TEAM" --assignee "$LINEAR_ASSIGNEE"
              --no-interactive --title "$title" --label "$repo" --label "$type")
  for l in "${extra_labels[@]:-}"; do [[ -n "$l" ]] && args+=(--label "$l"); done
  if [[ -n "$body_file" ]]; then args+=(--description-file "$body_file")
  elif [[ -n "$body" ]];   then args+=(--description "$body"); fi

  linear "${args[@]}"
}

cmd_list() {
  local all=0 repo=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all)  all=1; shift ;;
      --repo) repo="$2"; shift 2 ;;
      *) die "list: unknown arg '$1'" ;;
    esac
  done
  [[ -z "$repo" ]] && repo="$(repo_label || true)"
  local args=(issue mine --team "$LINEAR_TEAM" --sort priority)
  if [[ $all -eq 1 ]]; then
    args+=(--all-states)
  else
    # "open" = Todo + In Progress + In Review (exclude Backlog/Done/Canceled)
    args+=(-s unstarted -s started)
  fi
  [[ -n "$repo" ]] && args+=(--label "$repo")
  linear "${args[@]}"
}

cmd_move() {
  local id="${1:-}" state="${2:-}"
  [[ -n "$id" && -n "$state" ]] || die "move: usage: move SB-N \"State\""
  linear issue update "$id" -s "$state"
}

# Add "Fixes SB-N" to a PR body (idempotent). PR# optional — defaults to the
# open PR for the current branch.
cmd_link() {
  local id="${1:-}" pr="${2:-}"
  [[ -n "$id" ]] || die "link: usage: link SB-N [PR#]"
  command -v gh >/dev/null || die "link: gh CLI not found"
  local sel=(); [[ -n "$pr" ]] && sel=("$pr")
  local body
  body="$(gh pr view "${sel[@]}" --json body --jq .body 2>/dev/null)" || die "link: no PR found (specify PR#)"
  if grep -qiE "(fixes|closes|ref) ${id}\b" <<<"$body"; then
    echo "link: PR already references ${id}"; return 0
  fi
  gh pr edit "${sel[@]}" --body "${body}"$'\n\n'"Fixes ${id}"
  echo "link: added 'Fixes ${id}' to PR body"
}

cmd_audit_unassigned() {
  local fix=0; [[ "${1:-}" == "--fix" ]] && fix=1
  local ids
  ids="$(linear api "{ issues(first:250, filter:{ team:{ key:{ eq:\"${LINEAR_TEAM}\" } }, assignee:{ null:true } }) { nodes { identifier } } }" \
        | grep -oE "${LINEAR_TEAM}-[0-9]+" | sort -u || true)"
  if [[ -z "$ids" ]]; then echo "audit: no unassigned ${LINEAR_TEAM} issues 🎉"; return 0; fi
  echo "Unassigned ${LINEAR_TEAM} issues:"; echo "$ids" | sed 's/^/  /'
  if [[ $fix -eq 1 ]]; then
    while read -r id; do
      [[ -z "$id" ]] && continue
      linear issue update "$id" --assignee "$LINEAR_ASSIGNEE" >/dev/null && echo "  ✓ assigned $id"
    done <<<"$ids"
  else
    echo "(re-run with --fix to assign them all to ${LINEAR_ASSIGNEE})"
  fi
}

sub="${1:-}"; shift || true
case "$sub" in
  repo-label)        cmd_repo_label "$@" ;;
  new)               cmd_new "$@" ;;
  list)              cmd_list "$@" ;;
  move)              cmd_move "$@" ;;
  link)              cmd_link "$@" ;;
  audit-unassigned)  cmd_audit_unassigned "$@" ;;
  ""|-h|--help) sed -n '2,28p' "$0" ;;
  *) die "unknown subcommand '$sub' (try --help)" ;;
esac
