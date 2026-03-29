## Branch Protection Standards

All repositories should have branch protection enabled on the `main` branch:

### Required Settings
- **Require a pull request before merging**: Yes
- **Require approvals**: No (single-user repos - creator must be able to merge their own PRs)
- **Require status checks to pass before merging**: Yes (when CI exists)

### GitOps CI Access
- CI workflows that commit to main (e.g., image tag updates) must use a Fine-grained PAT (`GH_PAT` secret) with `contents: write` permission
- PAT owner permissions bypass branch protection
- Always include `[skip ci]` in automated commits to prevent loops

### Why This Matters
- Audit trail: All changes go through PRs
- Habit building: Even solo developers benefit from PR workflow
- GitOps compatibility: Allows automated tag updates while maintaining protection

@RTK.md
