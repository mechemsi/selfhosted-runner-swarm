# /review — Code Review

Review changed files against project conventions and report findings.

## Steps

1. Run `git diff --name-only` to find changed files
2. Read each changed file
3. Check against rules in `.claude/rules/`
4. Report findings grouped by severity:
   - **Critical**: Security issues, broken logic, data loss risks
   - **Warning**: Convention violations, missing error handling, resource leaks
   - **Suggestion**: Style improvements, readability, documentation gaps
5. Include file path and line number for each finding
6. End with overall verdict: APPROVE / REQUEST_CHANGES
