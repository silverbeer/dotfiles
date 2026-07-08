#!/usr/bin/env bash
# linear-crud skill helper — thin wrapper around the `linear` CLI that bakes in
# the silverbeer workspace conventions so Claude never has to remember them.
#
# Conventions enforced:
#   - team     = SB (override: LINEAR_TEAM)
#   - assignee = silverbeer.io, ALWAYS (override: LINEAR_ASSIGNEE)
#   - repo label (group) detected from the current git repo
#   - type label (group) required on create
#   - epic = Linear project; every epic MUST map to a repo label (epic_repo)
#
# Subcommands:
#   repo-label                  print the repo label inferred from cwd
#   epics                       list Linear projects (epics) + their mapped repo
#   stats [--days N]            momentum dashboard (totals, velocity, age, sparkline)
#   new   --title T (--body B | --body-file F) --type TYPE [--repo R] [--epic E] [--label L ...]
#   list  [--all] [--repo R] [--epic E]   my issues (open by default)
#   move  SB-N "State"          change workflow state
#   link  SB-N [PR#]            add "Fixes SB-N" to a PR body
#   audit-unassigned [--fix]    SB issues not assigned to the default assignee
#
# CLI surface (linear 2.0.0, verified 2026-06-20 — this build differs sharply
# from the 2026-05-30 one; do NOT trust older notes):
#   - subcommand is `issue` (SINGULAR); `issues` (plural) just dumps usage.
#   - `issue mine` / `issue list` / `issue l` are ALIASES of one command: MY
#     issues. Needs --team. Filters: --state (repeatable, TYPE values
#     triage|backlog|unstarted|started|completed|canceled), --all-states,
#     --project, --project-label, -l/--label (repeatable), --sort
#     manual|priority (REQUIRED — no default), --limit (0=unlimited). No --json.
#   - `issue query` is the all-assignees query engine: --assignee, -U/--unassigned,
#     --all-teams, --search, --project, -l/--label, --all-states (default), and
#     -j/--json. Used by audit-unassigned.
#   - `issue create`: -t/--title, -a/--assignee <username>, -l/--label
#     (REPEATABLE, not a comma list), --description / --description-file,
#     --team, --project, --no-interactive, -s/--state, -p/--priority.
#   - `issue update <id>`: -a/--assignee, -s/--state, -t/--title, -l/--label,
#     --project, --description/-file. (state accepts a name or type.)
#   - `issue view <id>` shows one issue; `issue comment add <id>` comments.
#   - `project list --team SB [-j]` lists projects; `project view <slug>` for one.
set -euo pipefail

LINEAR_TEAM="${LINEAR_TEAM:-SB}"
LINEAR_ASSIGNEE="${LINEAR_ASSIGNEE:-silverbeer.io}"

die() { echo "error: $*" >&2; exit 1; }

# Strip ANSI color codes from CLI output (the binary colorizes even when piped).
strip_ansi() { sed -E 's/\x1b\[[0-9;]*m//g'; }

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
    trd*|*investment*)         echo "TRD" ;;
    *) return 1 ;;
  esac
}

# Epic (Linear project) -> repo label. EVERY epic MUST resolve to a repo here —
# that is the required "project-level label". To add an epic, give it a repo.
# (Inferred 2026-06-20 from each project's existing issue labels.)
epic_repo() {
  local key; key="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$key" in
    *goals*|*multi-metric*)                 echo "STK" ;;
    *byok*|*multi-source*|*import*)         echo "STK" ;;
    *social*|*community*)                   echo "STK" ;;
    *athlete*|*training*)                   echo "STK" ;;
    *local*agent*|*automation*)             echo "STK" ;;
    *quality*|*ci*)                         echo "STK" ;;
    *vision*|*roadmap*)                     echo "STK" ;;
    *trd*|*investment*)                     echo "TRD" ;;
    *) return 1 ;;
  esac
}

cmd_repo_label() { repo_label || die "unknown repo '$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")' — pass --repo explicitly"; }

# List Linear projects (epics) with the repo each maps to. Names shown here are
# the exact strings to pass to --epic / --project. Driven off JSON (jq) so wide
# names don't get mangled by the text table's column padding.
cmd_epics() {
  command -v jq >/dev/null || die "epics: jq not found"
  echo "Epics (Linear projects) — pass the NAME to --epic:"
  linear project list --team "$LINEAR_TEAM" -j 2>/dev/null \
    | jq -r '.nodes[] | "\(.name)\t\(.status.name // "?")"' \
    | while IFS=$'\t' read -r name status; do
        local repo; repo="$(epic_repo "$name" 2>/dev/null || echo '??')"
        printf '  [%-3s] %-32s %s\n' "$repo" "$name" "$status"
      done
  echo
  echo "([??] = epic not yet mapped to a repo in epic_repo(); add it before use.)"
}

cmd_new() {
  local title="" body="" body_file="" type="" repo="" epic="" extra_labels=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --title)     title="$2"; shift 2 ;;
      --body)      body="$2"; shift 2 ;;
      --body-file) body_file="$2"; shift 2 ;;
      --type)      type="$2"; shift 2 ;;
      --repo)      repo="$2"; shift 2 ;;
      --epic)      epic="$2"; shift 2 ;;
      --label)     extra_labels+=("$2"); shift 2 ;;
      *) die "new: unknown arg '$1'" ;;
    esac
  done
  [[ -n "$title" ]] || die "new: --title required"
  [[ -n "$type"  ]] || die "new: --type required (bug|feature|chore|docs|infra|security)"

  # Epic enforces its repo label (the project-level label). If both --epic and
  # --repo are given they must agree.
  if [[ -n "$epic" ]]; then
    local epic_r
    epic_r="$(epic_repo "$epic" || die "new: epic '$epic' is not mapped to a repo — add it to epic_repo() in this helper (run 'epics' to see the list)")"
    if [[ -n "$repo" && "$repo" != "$epic_r" ]]; then
      die "new: --repo $repo conflicts with epic '$epic' (repo $epic_r)"
    fi
    repo="$epic_r"
  fi
  [[ -z "$repo" ]] && repo="$(repo_label || die "new: could not detect repo — pass --repo or --epic")"

  # Resolve the description body (file wins over inline).
  if [[ -n "$body_file" ]]; then
    [[ -f "$body_file" ]] || die "new: body file not found: $body_file"
  fi

  # Labels are REPEATED -l flags on this CLI (repo + type + any area labels).
  local args=(issue create --title "$title" --team "$LINEAR_TEAM"
              --assignee "$LINEAR_ASSIGNEE" --no-interactive
              -l "$repo" -l "$type")
  local l; for l in "${extra_labels[@]:-}"; do [[ -n "$l" ]] && args+=(-l "$l"); done
  [[ -n "$epic" ]] && args+=(--project "$epic")
  if [[ -n "$body_file" ]]; then
    args+=(--description-file "$body_file")
  elif [[ -n "$body" ]]; then
    args+=(--description "$body")
  fi

  linear "${args[@]}"
}

cmd_list() {
  local all=0 repo="" epic=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all)  all=1; shift ;;
      --repo) repo="$2"; shift 2 ;;
      --epic) epic="$2"; shift 2 ;;
      *) die "list: unknown arg '$1'" ;;
    esac
  done

  # An epic implies its repo label; otherwise fall back to cwd detection.
  if [[ -n "$epic" ]]; then
    repo="$(epic_repo "$epic" || die "list: epic '$epic' is not mapped to a repo (run 'epics')")"
  fi
  [[ -z "$repo" && -z "$epic" ]] && repo="$(repo_label || true)"

  # `mine` = my issues; --sort is required. Open = unstarted + started.
  local args=(issue mine --team "$LINEAR_TEAM" --sort priority --limit 0 --no-pager)
  if [[ $all -eq 1 ]]; then
    args+=(--all-states)
  else
    args+=(--state unstarted --state started)
  fi
  [[ -n "$repo" ]] && args+=(-l "$repo")
  [[ -n "$epic" ]] && args+=(--project "$epic")
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

# Unassigned audit. `issue mine/list` only ever return MY issues (they are
# aliases), so a diff is useless — use `issue query -U`, which filters to
# unassigned issues across all assignees directly.
cmd_audit_unassigned() {
  local fix=0; [[ "${1:-}" == "--fix" ]] && fix=1
  local ids
  ids="$(linear issue query --team "$LINEAR_TEAM" --unassigned --all-states --limit 0 --no-pager 2>/dev/null \
        | strip_ansi | grep -oE "${LINEAR_TEAM}-[0-9]+" | sort -u || true)"
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

# Momentum dashboard. Pulls every SB issue once (issue query -j) and crunches it
# with jq. No completedAt field exists on this CLI, so "ship time" / "closed in
# window" use updatedAt as the close-time proxy (flagged approx in the output).
cmd_stats() {
  command -v jq >/dev/null || die "stats: jq not found"
  local days=7
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --days) days="$2"; shift 2 ;;
      *) die "stats: unknown arg '$1'" ;;
    esac
  done

  local json
  json="$(linear issue query --team "$LINEAR_TEAM" --all-states --limit 0 -j 2>/dev/null)" \
    || die "stats: query failed"
  [[ -n "$json" ]] || die "stats: empty result"

  jq -r --argjson win "$days" --arg team "$LINEAR_TEAM" '
    def age($iso): ($iso | sub("\\.[0-9]+Z$";"Z") | fromdateiso8601) as $t | (now - $t)/86400;
    def r1: (.*10|round)/10;
    def bar($n; $max): ["▁","▂","▃","▄","▅","▆","▇","█"] as $b
      | if $max<=0 then " " else $b[(($n/$max)*7|floor)] end;
    ["MT","MS","MSA","QB","STK","TODO","TRD"] as $repos

    | .nodes as $all
    | ($all|length) as $total
    | [ $all[] | select(.state.type=="completed") ] as $done
    | [ $all[] | select(.state.type=="canceled") ]  as $cancel
    | [ $all[] | select(.state.type|IN("backlog","unstarted","started","triage")) ] as $open
    | [ $open[] | select(.state.type=="started") ] as $active
    | [ $open[] | select(.state.type=="backlog") ] as $backlog
    | [ $open[] | select(.state.type|IN("unstarted","triage")) ] as $todo

    | [ $all[]  | select(age(.createdAt) <= $win) ]  as $created_win
    | [ $done[] | select(age(.updatedAt) <= $win) ]  as $closed_win
    | (($created_win|length) - ($closed_win|length)) as $net

    | (if ($open|length)>0 then ([ $open[] | age(.createdAt) ]|add/(. as $x|$open|length)) else 0 end) as $avg_open
    | (if ($done|length)>0 then ([ $done[] | (age(.createdAt) - age(.updatedAt)) ]|add/(. as $x|$done|length)) else 0 end) as $avg_ship
    | ($open | max_by(age(.createdAt))) as $oldest

    # per-repo and per-epic counts (desc, top 6)
    | ( [ $all[] | (.labels.nodes[].name | select(. as $n|$repos|index($n))) ] | group_by(.)
        | map({k:.[0], n:length}) | sort_by(-.n) ) as $by_repo
    | ( [ $all[] | .project.name | select(.) ] | group_by(.)
        | map({k:.[0], n:length}) | sort_by(-.n)[0:6] ) as $by_epic

    # created-per-day sparkline, last 14 days (bucket 0 = today)
    | ( [ range(0;14) as $d
          | { d:$d, n: ([ $all[] | select((age(.createdAt)|floor)==$d) ] | length) } ]
        | reverse ) as $spark
    | ( $spark | map(.n) | max ) as $smax

    | "🏔  SILVERBEER MOMENTUM — \($team) team",
      "────────────────────────────────────────────",
      "  ✅ Shipped     \($done|length)   (\((($done|length)/$total*100)|floor)% of all tickets)",
      "  🔨 Open        \($open|length)   (\($active|length) in progress · \($todo|length) todo · \($backlog|length) backlog)",
      "  🗑  Canceled    \($cancel|length)",
      "  📦 Total ever  \($total)",
      "",
      "  Last \($win)d:   +\($created_win|length) created   −\($closed_win|length) shipped   → net \(if $net>=0 then "+" else "" end)\($net)",
      "  Avg age (open):  \($avg_open|r1)d        Oldest open: \(if $oldest then "\($oldest.identifier) (\(age($oldest.createdAt)|floor)d)" else "—" end)",
      "  Avg ship time:   \($avg_ship|r1)d  (approx, by last-update)",
      "",
      "  By repo:  \($by_repo | map("\(.k) \(.n)") | join("  ·  "))",
      "  By epic:  \($by_epic | map("\(.k|.[0:18]) \(.n)") | join("  ·  "))",
      "",
      "  Created/day (14d, →today):  \($spark | map(bar(.n;$smax)) | join(""))   peak \($smax)",
      ""
  ' <<<"$json"

  # Motivational closer (computed in jq above would be awkward; do it here).
  local shipped created_win closed_win
  shipped="$(jq '[.nodes[]|select(.state.type=="completed")]|length' <<<"$json")"
  created_win="$(jq --argjson w "$days" '[.nodes[]|select(((.createdAt|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601)) as $t|(now-$t)/86400 <= $w)]|length' <<<"$json")"
  closed_win="$(jq --argjson w "$days" '[.nodes[]|select(.state.type=="completed")|select(((.updatedAt|sub("\\.[0-9]+Z$";"Z")|fromdateiso8601)) as $t|(now-$t)/86400 <= $w)]|length' <<<"$json")"
  local msg
  if   (( closed_win >= 5 )); then msg="🔥 ${closed_win} shipped in ${days} days — you are ON FIRE. Keep crushing."
  elif (( closed_win >= 1 )); then msg="💪 ${closed_win} shipped in ${days} days. Momentum is real — push the next one."
  elif (( created_win >= 1 )); then msg="🌱 ${created_win} new ideas logged. Now go close one. 🎯"
  else msg="🧗 Quiet ${days} days. ${shipped} lifetime ships say you've got this — pick one and go."; fi
  echo "  ${msg}"
  echo
}

sub="${1:-}"; shift || true
case "$sub" in
  repo-label)        cmd_repo_label "$@" ;;
  epics)             cmd_epics "$@" ;;
  stats)             cmd_stats "$@" ;;
  new)               cmd_new "$@" ;;
  list)              cmd_list "$@" ;;
  move)              cmd_move "$@" ;;
  link)              cmd_link "$@" ;;
  audit-unassigned)  cmd_audit_unassigned "$@" ;;
  ""|-h|--help) sed -n '2,33p' "$0" ;;
  *) die "unknown subcommand '$sub' (try --help)" ;;
esac
