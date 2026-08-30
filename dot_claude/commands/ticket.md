Start work on a Linear ticket end-to-end: fetch it, branch, plan. Usage: `/ticket SB-123` (or `/ticket` to pick from the current sprint).

This is the light version — it sets you up, then you drive. For the full
`dev-engineer` → `qe-engineer` → `/code-review` → PR loop, use `/work SB-123`.

## Steps

1. **Fetch the issue** using the `linear-crud` skill conventions:
   ```bash
   bash ~/.claude/skills/linear-crud/scripts/linear.sh pack SB-<n>
   ```
   That is one brief JSON (issue, branch name, repo label, git state, PR). Use
   `linear.sh view --full SB-<n>` only when the acceptance-criteria text is needed.
   If no ticket number was given, list the user's open issues for this repo and ask which one.

2. **Create the branch** from up-to-date main:
   ```bash
   git checkout main && git pull
   bash ~/.claude/skills/linear-crud/scripts/linear.sh branch SB-<n>
   ```
   Use `linear.sh branch`, not a hand-rolled `git checkout -b` — it guarantees
   the `sb-<n>` token Linear keys on (details: linear-crud SKILL.md, "The
   delivery loop").

3. **Restate the acceptance criteria** in 2–4 bullets from the issue description. If the description is thin, say so and ask one clarifying question before coding — don't guess scope.

4. **Plan before touching code.** For anything beyond a trivial fix, delegate exploration to an Explore/investigator subagent (keeps the main context small), then present a short plan.

6. When the work is done, finish with `/cppp` — the PR title should reference the ticket (e.g. `feat: thing (SB-123)`), matching this repo's commit history style.
