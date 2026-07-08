Wrap up the current session cleanly so the user can start a FRESH session without losing context. Long-lived sessions are the #1 token burner (every turn re-reads the whole accumulated context), so this command makes "start fresh" cheap.

## Steps

1. **Capture state.** Summarize the session's durable facts:
   - What was accomplished (PRs, commits, tickets moved — with IDs/URLs)
   - What is in-flight or half-done (branch names, failing tests, next step)
   - Any decisions made that aren't yet reflected in code or tickets

2. **Persist it where the next session will find it:**
   - In-flight work tied to a ticket → comment on the Linear issue (`linear-crud` skill)
   - Project-level facts/decisions → auto-memory (`memory/` dir, update MEMORY.md index)
   - Uncommitted code → tell the user; offer /cppp or a WIP commit on the feature branch. Never leave work only in the working tree without flagging it.

3. **Verify nothing is stranded:** run `git status` — flag untracked/modified files the summary didn't mention.

4. **Tell the user:** print a short handoff summary (5 lines max) and remind them to start a new session for the next task. If the next task is known, give them a one-line prompt they can paste into the fresh session.

Keep it tight — this is a checkpoint, not a report.
