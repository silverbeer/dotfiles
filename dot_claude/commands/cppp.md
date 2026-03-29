Create a commit and pull request following standard GitOps conventions.

## Creating a Commit

1. Run `git status` and `git diff` to understand what changed
2. Stage only relevant files — never `git add .` or `git add -A` blindly
3. Check recent commits with `git log --oneline -5` to match the repo's commit style
4. Write a conventional commit message:
   - `feat:` — new feature
   - `fix:` — bug fix
   - `refactor:` — restructuring without behavior change
   - `chore:` — maintenance, deps, config
   - `docs:` — documentation only
   - `test:` — adding or fixing tests
   - `ci:` — CI/CD pipeline changes
5. Always append co-authorship:
   ```
   Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
   ```
6. Use a HEREDOC for the commit message to preserve formatting

## Creating a PR

1. Push the branch: `git push -u origin <branch-name>`
2. Create PR with `gh pr create` using this body structure:
   ```
   ## Summary
   - <bullet points of what changed and why>

   ## Test plan
   - [ ] <what to verify>

   🤖 Generated with Claude Code
   ```
3. Return the PR URL to the user

## Branch hygiene

- Never commit directly to `main` — always use a feature branch
- Branch naming: `feat/description`, `fix/description`, `chore/description`
- Check current branch before starting: `git branch --show-current`
- If on main, create a branch first: `git checkout -b feat/your-feature`

## After PR is merged

- Do NOT manually trigger builds or deployments
- Each project has its own CI/CD pipeline — check the project's CLAUDE.md for specifics
- If the user asks about deployment status, check the project-specific deployment commands

## Safety rules

- Never force push to main
- Never use `--no-verify` to skip hooks unless user explicitly requests it
- Never commit `.env` files, secrets, or credentials
- Prefer `git restore <file>` over `git checkout` for discarding changes
