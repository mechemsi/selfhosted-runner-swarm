---
name: security-review
description: Security audit for rorch runner infrastructure
triggers:
  - security
  - audit
  - vulnerability
---

# Security Review Skill

Triggered when working on security-sensitive areas of the orchestrator.

## Checklist

### Secrets management
- [ ] PATs only in `.env` or `config.yml` (both gitignored)
- [ ] No secrets in Dockerfiles or docker-compose.yml
- [ ] Registration tokens are short-lived and cleaned up

### Container isolation
- [ ] Memory limits set (`--memory`)
- [ ] PID limits set (`--pids-limit 512`)
- [ ] CPU limits documented (0 = unlimited, warn user)
- [ ] Containers run as non-root user (`runner`)

### Network security
- [ ] GitHub API calls use HTTPS
- [ ] No unnecessary ports exposed
- [ ] Runner containers don't expose services

### Docker socket
- [ ] Mount is documented as root-equivalent access
- [ ] Only orchestrator has socket access
- [ ] Runner socket access is opt-in via config

### Input validation
- [ ] Config YAML parsing handles malformed input
- [ ] Environment variable interpolation is safe
- [ ] Container names are sanitized (no injection)
