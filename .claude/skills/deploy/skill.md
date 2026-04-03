---
name: deploy
description: Pre-deployment verification for rorch orchestrator
triggers:
  - deploy
  - release
  - ship
---

# Deploy Skill

Run this checklist before deploying changes.

## Phase 1: Code Quality
- [ ] Python syntax valid: `python3 -m py_compile orchestrator/orchestrator.py`
- [ ] Shell scripts valid: `shellcheck runner-image/entrypoint.sh`
- [ ] No TODO or FIXME in changed code

## Phase 2: Docker Builds
- [ ] Runner image builds: `docker build -t gh-runner:latest ./runner-image`
- [ ] Orchestrator builds: `docker compose build orchestrator`

## Phase 3: Configuration
- [ ] `example.config.yml` is valid YAML
- [ ] `.env.example` documents all required variables
- [ ] No secrets in committed files

## Phase 4: Security
- [ ] No hardcoded credentials
- [ ] Resource limits configured in example config
- [ ] Docker socket mount documented

## Phase 5: Documentation
- [ ] README.md reflects current behavior
- [ ] CLAUDE.md reflects current architecture
- [ ] Config examples match actual config schema

## Phase 6: Deploy
- [ ] `docker compose up -d --build`
- [ ] Verify logs: `docker compose logs -f orchestrator`
- [ ] Confirm runners appear in GitHub org/repo settings
