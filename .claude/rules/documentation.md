# Documentation Rules

## claudedocs/ directory
- `plans/` — feature specs BEFORE implementation
- `implementations/` — documentation AFTER building
- `decisions/` — Architecture Decision Records (ADRs)
- `runbooks/` — step-by-step operational guides

## Naming conventions
- Plans: `YYYY-MM-DD-short-name.md`
- Implementations: `YYYY-MM-DD-short-name.md`
- Decisions: `NNN-short-name.md` (sequential number)
- Runbooks: `short-name.md`

## Frontmatter
Every doc must have YAML frontmatter:
```yaml
---
title: Short descriptive title
status: draft | active | completed | deprecated
date: YYYY-MM-DD
related: [other-doc.md]
---
```

## INDEX.md
- Update `claudedocs/INDEX.md` when adding or changing docs
- Keep entries as one-line summaries with links

## When to create docs
- **Plan**: Before implementing a new feature or significant change
- **Implementation**: After completing a feature, documenting what was built
- **Decision**: When choosing between approaches with trade-offs
- **Runbook**: For any repeated operational process
