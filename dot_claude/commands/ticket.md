Start work on a Linear ticket end-to-end: fetch it, branch, plan. Usage: `/ticket SB-123` (or `/ticket` to pick from the current sprint).

## Steps

1. **Fetch the issue** using the `linear-crud` skill conventions:
   ```bash
   bash ~/.claude/skills/linear-crud/scripts/linear.sh view SB-<n>
   ```
   If no ticket number was given, list the user's open issues for this repo and ask which one.

2. **Move it to In Progress** (confirm with the user first, per linear-crud rules).

3. **Create the branch** from up-to-date main:
   ```bash
   git checkout main && git pull && git checkout -b <type>/sb-<n>-<short-slug>
   ```
   `<type>` from the issue's type label: feature→`feat`, bug→`fix`, chore→`chore`, docs→`docs`, infra→`ci`.

4. **Restate the acceptance criteria** in 2–4 bullets from the issue description. If the description is thin, say so and ask one clarifying question before coding — don't guess scope.

5. **Plan before touching code.** For anything beyond a trivial fix, delegate exploration to an Explore/investigator subagent (keeps the main context small), then present a short plan.

6. When the work is done, finish with `/cppp` — the PR title should reference the ticket (e.g. `feat: thing (SB-123)`), matching this repo's commit history style.
