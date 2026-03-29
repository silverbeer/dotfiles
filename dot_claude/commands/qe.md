Run a QE coverage audit on the current project and produce an actionable report.

Steps:
1. Detect the project type (look for pyproject.toml, package.json, go.mod, etc.)
2. Run the test suite and capture pass/fail counts
3. Run coverage analysis for the detected stack:
   - Python/pytest: `uv run pytest tests/ --cov=src --cov-report=term-missing -q`
   - Node/Jest: `npm test -- --coverage`
   - Go: `go test ./... -cover`
4. Parse the output and identify every module below 80% coverage
5. For each under-covered module, list the specific uncovered functions
6. Check for open GitHub issues labeled `test` or `coverage` — note which gaps are already tracked vs untracked
7. Output a prioritized action list:
   - CRITICAL (0% coverage, business logic)
   - HIGH (<50% coverage)
   - MEDIUM (50–80% coverage)
   - OK (>80% — call these out as wins)

Format as a clean markdown table followed by a numbered TODO list.
End with: "To fix the top gap now, say: @qe-engineer write tests for <module>"
