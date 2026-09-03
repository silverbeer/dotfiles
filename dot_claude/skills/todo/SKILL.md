---
name: todo
description: >-
  Capture, review, and complete personal todos — and add calendar events with
  contact-alias invites — via the `todo` CLI (AI-enriched, gamified task
  manager). Use when the user wants to add/remember a task, see what's on their
  list, check stats/streak, mark something done (optionally with a note), or
  add/list a calendar event — e.g. "add a todo", "remember to X", "what's on my
  list", "mark task 12 done", "add an event dinner friday 7pm", "how many points
  do I have". Also use proactively to offer capturing a follow-up task that
  surfaced during a session (ask first).
---

# Todo

Drive the `todo` CLI to manage the user's personal task list. The CLI is the
source of truth (local DuckDB at `~/.local/share/todo/todos.db`). Always use
the `--json` flag so output is machine-readable; parse stdout as JSON (status
text goes to stderr).

## Prerequisite

`todo` must be on PATH. Verify with `todo version`. If "command not found",
install from the repo:

```bash
uv tool install --editable ~/gitrepos/todo
```

(Per-machine: data does NOT sync across machines — each Mac has its own DB.)

## Capture a task

```bash
todo add "<task>" --json                 # with AI enrichment (category/priority/size)
todo add "<task>" --no-ai --json         # instant, skip the AI call (faster)
todo add "<task>" --due "EOW" --json     # set a due date on create
```

- Use the user's wording for `<task>`. Add `--desc "<detail>"` if they gave more context.
- Default to AI enrichment. Use `--no-ai` only when the user wants speed or the task is trivial.
- `--due/-D` accepts natural language or any date format (see Due dates below).
- Returns: `{id, title, status, priority, size, category, due_date, enrichment, ...}`.
- Report back the new id and any AI-suggested category/priority.

## Due dates

Set on create with `--due`, or change later with the `due` command:

```bash
todo due <id> "next monday" --json   # set / change
todo due <id> 07/04/2026 --json      # any date format
todo due <id> --clear --json         # remove the due date
```

Accepted expressions (case-insensitive): `today`/`EOD`, `tomorrow`, `EOW`/`end
of week` (Friday), `EOM`/`end of month`, `EOY`, weekday names (`monday`,
`on friday`), `next <weekday>`, `next week`, `in N days`, and explicit dates
(`6/11`, `07/04/2026`, `2026-07-04`, `July 4`). A bare month/day already past
rolls to next year. `todo ls` shows a Due column; overdue dates render red and
`is_overdue` is set in JSON.

## Review the list

```bash
todo ls --json            # active todos (default limit 10)
todo ls --limit 50 --json # more
todo ls --all --json      # include completed
```

Returns `{todos: [{id, title, status, priority, size, category, due_date, is_overdue, ai_enriched, ...}]}`.
Render a short summary for the user (id + title + priority). Flag any `is_overdue: true`.

## Complete a task

User may give an id or a description. If a description:

1. `todo ls --json` to fetch active todos.
2. Fuzzy-match the title. If exactly one clear match, proceed. If ambiguous or
   no match, show candidates and ask which id — do NOT guess.
3. Complete (one or many ids):

```bash
todo done <id> [<id> ...] --json
todo done <id> --note "paid $240, conf #8891" --json   # attach a completion note
```

Returns `{completed: [ids], failed: [ids], points_earned, achievements: [...]}`.
Celebrate points/achievements briefly if any were unlocked. `--note/-n` stores a
note on the completed todo (applied to each id); it shows in `todo show` and as
`completion_note` in JSON.

## Delete a task

Use when the user wants a task **removed entirely** (mistake, junk, no longer
relevant) — NOT finished. Unlike `done`, delete awards no points and is
irreversible. Resolve a description to an id the same way as `done` (list +
fuzzy match; ask if ambiguous).

```bash
todo delete <id> [<id> ...] --force --json   # --force is required in --json mode
```

Returns `{deleted: [ids], failed: [ids]}`. Prefer `done` over `delete` when the
user actually completed the task — only delete on clear intent to discard.

## Calendar events

Events are separate from todos (they have a start time and optional invitees).
They sync **one-way to Google Calendar** when the user is authenticated
(`todo calendar auth`); `todo event ls` reads the local DB only (no pull-back).

```bash
# AI mode (default): parses date/time/location/invitees from natural language
todo event add "dinner with parents friday 7pm at their place, invite wife and kids" --json
# Flag mode: explicit, no AI
todo event add "Soccer" --when "2026-06-13 10:00" --duration 90 --invite kids --no-ai --json

todo event ls --json                 # upcoming (add --all for past + cancelled)
todo event show <id> --json
todo event cancel <id> --json        # keeps history, drops from default list; un-syncs from Google
todo event delete <id> --force --json
todo event sync [<id>] --json        # push unsynced events to Google
todo event invite <id> [aliases] --json   # (re)send invites for an event
```

- Default to AI mode — give it the user's full phrasing. Dates resolve the same
  way as due dates (weekday names, `tomorrow`, etc.); times like `7pm` are kept.
- When authenticated, `event add` pushes to Google automatically (use
  `--no-sync` to keep it local). `is_synced: true` means it's on Google.
- **Invites email real people.** `--invite` resolves through contact aliases.
  Sending only happens with `--yes/-y` or an interactive confirm; in JSON/
  non-interactive mode without `--yes` the event syncs but no email is sent —
  surface this and let the user confirm before adding `--yes`.
- Returns event dicts: `{id, title, start_at, end_at, all_day, location, status,
  attendees, is_synced, google_event_id, ...}`.

## Google Calendar auth

```bash
todo calendar status --json   # {has_credentials, authenticated, ...}
todo calendar auth            # one-time OAuth (opens a browser; user runs this)
```

Needs an OAuth client credentials.json at `~/.config/todo/gcal_credentials.json`
(per machine). If `authenticated` is false, events stay local — tell the user to
run `todo calendar auth`.

## Contacts (invite aliases)

Aliases map a name to one or more emails so invites can say "wife"/"kids".

```bash
todo contact add wife jane@example.com --json
todo contact add kids sam@example.com alex@example.com --json   # alias -> many
todo contact ls --json
todo contact rm <alias> --json
```

When a user references a person to invite who isn't a known alias, ask for the
email and offer to `contact add` it for next time.

## Stats / progress

```bash
todo stats --json
```

Returns `{total_points, level, points_to_next_level, current_streak,
longest_streak, total_completed, daily_goal, tasks_completed_today,
daily_goal_met, points_earned_today}`.

## Detail of one task

```bash
todo show <id> --json
```

Returns the full todo plus `enrichment` (AI analysis) and `completion_note` if
present.

## Notes

- Errors come back as `{"error": "..."}` on stdout with exit 0 — check for an
  `error` key before assuming success.
- For anything not covered here (`dashboard`, `achievements`, `goal`, `enrich`),
  the human-readable command works without `--json`.
