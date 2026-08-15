---
title: Documentation Index
status: active
date: 2026-04-03
---

# claudedocs Index

Master index for project documentation.

## PRDs

- [Personal account all-repositories runner pool](prds/2026-07-11-personal-all-repositories.md) — Automatically cover repositories owned by a personal account.

## Plans

- [Implement personal account all-repositories runner pool](plans/2026-07-11-personal-all-repositories.md) — Add discovery and account-wide capacity allocation.

## Implementations

- [Personal account all-repositories runner pool](implementations/2026-07-11-personal-all-repositories.md) — Discovery, repository-scoped scheduling, and global capacity enforcement.
- [Observability and control dashboard](implementations/2026-08-15-observability-dashboard.md) — SQLite store, config overlay, Flask API and single-page UI; plus runner versioning, network isolation, Makefile and CI Docker builds.

## Decisions

- [001 — config.yml stays the default, the database is only an overlay](decisions/001-config-yml-stays-the-default.md) — Why runtime config edits are an overlay rather than a rewrite or a new source of truth.

## Runbooks
_No runbooks yet. Create one for repeated operational processes._
