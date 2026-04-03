# CLAUDE.md — Project Instructions for Claude Code

## Project overview

RORCH is a GitHub Actions runner orchestrator. It manages pools of ephemeral self-hosted runner containers, scaling them up/down based on GitHub Actions job queue demand.

## Tech stack

- **Orchestrator**: Python 3.12 (single-file application)
- **Runner image**: Ubuntu 22.04 + official GitHub Actions runner
- **Infrastructure**: Docker, Docker Compose
- **External API**: GitHub REST API v2022-11-28

## Project structure

```
rorch/
├── orchestrator/orchestrator.py    # Main application — all orchestration logic
├── orchestrator/Dockerfile         # Orchestrator container
├── runner-image/Dockerfile         # Runner container image
├── runner-image/entrypoint.sh      # Runner registration/lifecycle
├── docker-compose.yml              # Service definition
├── example.config.yml              # Pool configuration template
└── .env.example                    # Environment variables template
```

## Running the project

Everything runs in Docker. The orchestrator container manages runner containers via the Docker socket.

```bash
# Build runner image first
docker build -t gh-runner:latest ./runner-image

# Start orchestrator
docker-compose up -d

# View logs
docker-compose logs -f orchestrator
```

## Code conventions

- **Python**: Follow PEP 8. Use type hints for function signatures. Wrap errors with context.
- **Bash**: Use `set -e` in scripts. Quote all variable expansions. Check required env vars early.
- **Docker**: Multi-stage builds when beneficial. Pin base image versions. Minimize layers.
- **Config**: YAML for user-facing config. Environment variables for secrets.

## Architecture notes

- The orchestrator is a single Python process with one thread per pool
- Each pool polls GitHub API independently on `POLL_INTERVAL`
- Runners are ephemeral (`--ephemeral` flag) — one job per container
- Docker CLI is used directly (subprocess), not the Docker SDK
- Container naming: `gh-runner-{pool}-{uuid8}`
- Stuck containers (>3 min without registering) are killed automatically

## Key formulas

```
desired = min(max_runners, max(min_idle, busy + queued))
```

## Important constraints

- `config.yml` and `.env` are gitignored — they contain secrets
- Runner image must be built before starting the orchestrator
- Docker socket must be mounted for the orchestrator to manage containers
- GitHub PAT needs Actions (read/write) + Administration (read/write) permissions
- Runner GID (991) must match host's docker group GID

## Testing changes

- Test orchestrator changes: `docker-compose up --build -d`
- Test runner image changes: `docker build -t gh-runner:latest ./runner-image` then trigger a workflow
- Check logs for errors: `docker-compose logs -f orchestrator`

## What NOT to do

- Don't commit `.env` or `config.yml` (they contain PATs)
- Don't use the Docker Python SDK — the project uses CLI deliberately for simplicity
- Don't make runners non-ephemeral — the scaling logic depends on ephemeral behavior
- Don't change container naming format without updating the prefix-based filtering
