# Docker Rules

## Images
- Pin base image versions (e.g., `python:3.12-slim`, `ubuntu:22.04`)
- Minimize layers — combine related RUN commands
- Clean up apt caches in the same layer: `&& rm -rf /var/lib/apt/lists/*`
- Use `.dockerignore` if build context grows

## Compose
- Always use `restart: unless-stopped` for the orchestrator
- Mount config as read-only: `:ro`
- Set log rotation to prevent disk fill

## Runner containers
- Ephemeral only — never reuse runner containers
- Resource limits are mandatory in production pools
- Container naming must follow `gh-runner-{pool}-{uuid}` format
- PID limit (512) protects host from fork bombs

## Security
- Never bake secrets into images — use env vars or mounted files
- Docker socket mount gives root-equivalent access — document this risk
- Runner user should not run as root inside the container
