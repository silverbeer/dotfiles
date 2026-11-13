Create a commit and pull request following standard GitOps conventions.

## Branch first

- Never commit directly to `main`. Check: `git branch --show-current`
- Ticketed work: `bash ~/.claude/skills/linear-crud/scripts/linear.sh branch SB-N`
  — the branch name must carry `sb-N` or the Linear integration ignores the PR.
- No ticket yet: file an `adhoc` one first (linear-crud "Create"); work here is
  tracked in Linear. Only fall back to `feat/…`, `fix/…`, `chore/…` in repos
  outside the SB workflow.

## Creating a commit

1. Run `git status` and `git diff` to understand what changed
2. Stage only relevant files — never `git add .` or `git add -A` blindly
3. Check `git log --oneline -5` to match the repo's commit style
4. Write a conventional commit message (`feat:` `fix:` `refactor:` `chore:`
   `docs:` `test:` `ci:`), with `(SB-N)` at the end of the subject when ticketed
5. End with the co-author trailer **from the harness attribution reminder** —
   it carries the live model name, e.g.
   ```
   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
   ```
   Never copy a model name from this file or an old commit. With no reminder in
   context, use `Claude <model name from your system prompt>`; if that is
   unknown too, plain `Claude`.
6. Use a HEREDOC for the commit message to preserve formatting

## Creating the PR

1. `git push -u origin <branch>`
2. `gh pr create` (non-draft — that moves the ticket to In Progress) with this body:
   ```
   ## Summary
   - <why, then what — one line each>

   ## Test plan
   - [ ] <a check that will actually be run>

   Fixes SB-N

   <PR attribution line from the harness reminder>
   ```
3. Return the PR URL

### PR body budget

Reviewers read the diff; the body says what the diff can't.

- Summary: at most 3 bullets. Lead with *why*. Don't narrate the diff file by file.
- Test plan: only checks someone will run. No "verify it works".
- No headings beyond these two, no "Changes"/"Overview"/"Notes" sections, no restating the title.
- Ask before adding anything longer — a design note belongs in the ticket or `docs/`.

## Merge and clean up

Merge your own green PRs; don't wait to be asked.

```bash
gh pr checks <N> --watch                       # when CI exists
gh pr merge <N> --squash --delete-branch       # merge → ticket auto-moves to Done
git switch main && git pull --ff-only
git branch -D <branch>                         # squash merges need -D
linear.sh worktree-done SB-N                   # instead, if work was in a linear.sh worktree
jt git audit --repo .                          # confirm nothing of yours is left
```

- Do NOT manually trigger builds or deployments — each project's CI/CD handles
  it; see the project's CLAUDE.md.

## Safety rules

- Never force push to main
- Never use `--no-verify` to skip hooks unless user explicitly requests it
- Never commit `.env` files, secrets, or credentials
- Prefer `git restore <file>` over `git checkout` for discarding changes
