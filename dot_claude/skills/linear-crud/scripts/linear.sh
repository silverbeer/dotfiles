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
#   repo-label                 print the repo label inferred from cwd (MT/MS/MSA/QB/STK/TODO)
#   new   --title T (--body B | --body-file F) --type TYPE [--repo R] [--label L ...]
#   list  [--all] [--repo R]   my issues (open by default), scoped to current repo
#   move  SB-N "State"         change workflow state
#   link  SB-N [PR#]           add "Fixes SB-N" to a PR body (current branch's PR if # omitted)
#   audit-unassigned [--fix]   list SB issues not assigned to the default assignee; --fix assigns them
#
# CLI drift notes (CLI v2.0.0, verified 2026-06-10):
#   - subcommands are SINGULAR: `issue` and `label` (plural forms reject flags).
#   - `issue create` takes title via -t/--title, labels as REPEATED -l flags
#     (no --labels comma list), markdown body via --description-file, and
#     supports --no-interactive.
#   - `issue mine` (aka list/l) = YOUR issues: -l label (repeatable), -s/--state
#     (triage|backlog|unstarted|started|completed|canceled), --all-states,
#     --limit (0=unlimited). No --assignee/--json on this subcommand.
#   - `issue query` = ALL issues w/ filters: --assignee, -U/--unassigned, -l,
#     -s/--state, --all-states (default), -j/--json, --limit.
#   - `issue update` uses -t/--title, -s/--state (by name, e.g. "In Progress"),
#     -a/--assignee.
#   - `linear api '<graphql>'` WORKS — use it for label-group ops (e.g. create a
#     repo label under the `repo` group via issueLabelCreate parentId).
#   - view one issue: `issue view SB-N` (there is no `issue get`).
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
    todo)                      echo "TODO" ;;
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

  # Labels are repeated -l flags on this CLI: repo + type + any area labels.
  local label_args=(-l "$repo" -l "$type")
  for l in "${extra_labels[@]:-}"; do [[ -n "$l" ]] && label_args+=(-l "$l"); done

  # CLI v2.0.0: singular `issue create`, title via -t, repeated -l labels,
  # --description-file for markdown, --no-interactive to skip prompts.
  local args=(issue create -t "$title" --team "$LINEAR_TEAM"
              --assignee "$LINEAR_ASSIGNEE" "${label_args[@]}" --no-interactive)
  if [[ -n "$body_file" ]]; then
    [[ -f "$body_file" ]] || die "new: body file not found: $body_file"
    args+=(--description-file "$body_file")
  elif [[ -n "$body" ]]; then
    args+=(--description "$body")
  fi

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

  # `issue mine` lists YOUR issues. Default to open work (unstarted + started);
  # --all shows every state. Scope to the current repo's label when known.
  # `issue mine` requires an explicit --sort (no default).
  local args=(issue mine --team "$LINEAR_TEAM" --sort priority --limit 0)
  if [[ $all -eq 1 ]]; then
    args+=(--all-states)
  else
    args+=(-s unstarted -s started)
  fi
  [[ -n "$repo" ]] && args+=(-l "$repo")
  linear "${args[@]}"
}

cmd_move() {
  local id="${1:-}" state="${2:-}"
  [[ -n "$id" && -n "$state" ]] || die "move: usage: move SB-N \"State\""
  linear issue update "$id" --state "$state"
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

# Unassigned = SB issues with no assignee. `issue query -U` filters server-side
# (no id-diff workaround needed). Scope to actionable states (skip done/canceled).
cmd_audit_unassigned() {
  local fix=0; [[ "${1:-}" == "--fix" ]] && fix=1
  local ids
  ids="$(linear issue query --team "$LINEAR_TEAM" -U \
            -s triage -s backlog -s unstarted -s started --limit 0 -j 2>/dev/null \
          | grep -oE "${LINEAR_TEAM}-[0-9]+" | sort -u || true)"
  if [[ -z "$ids" ]]; then echo "audit: no unassigned ${LINEAR_TEAM} issues 🎉"; return 0; fi
  echo "Issues not assigned to ${LINEAR_ASSIGNEE}:"; echo "$ids" | sed 's/^/  /'
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
