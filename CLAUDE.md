# CLAUDE.md — Project Instructions for Claude Code

## Project overview

RORCH is a GitHub Actions runner orchestrator. It manages pools of ephemeral self-hosted runner containers, scaling them up/down based on GitHub Actions job queue demand.

## Tech stack

- **Orchestrator**: Python 3.12 (modular package)
- **Runner image**: Ubuntu 22.04 + official GitHub Actions runner
- **Infrastructure**: Docker, Docker Compose
- **External API**: GitHub REST API v2022-11-28
- **Linting**: ruff, pyright
- **Testing**: pytest, pytest-cov

## Project structure

```
rorch/
├── orchestrator/
│   ├── Dockerfile
│   ├── pyproject.toml           # Project config (ruff, pyright, pytest)
│   ├── requirements.txt         # Runtime dependencies
│   ├── rorch/                   # Python package
│   │   ├── __init__.py
│   │   ├── __main__.py          # Entry point (python -m rorch)
│   │   ├── config.py            # PoolConfig, YAML loading, validation
│   │   ├── protocols.py         # Protocol interfaces (DI)
│   │   ├── github_client.py     # GitHub REST API client
│   │   ├── docker_client.py     # Docker CLI container management
│   │   └── scaler.py            # Scaling logic (PoolScaler)
│   └── tests/
│       ├── conftest.py          # Shared fixtures
│       ├── test_config.py
│       ├── test_docker_client.py
│       └── test_scaler.py
├── runner-image/
│   ├── Dockerfile
│   └── entrypoint.sh
├── .github/workflows/ci.yml    # CI: ruff, pyright, pytest, docker build
├── docker-compose.yml
├── example.config.yml
└── .env.example
```

## Running the project

Everything runs in Docker.

```bash
docker build -t gh-runner:latest ./runner-image
docker-compose up -d
docker-compose logs -f orchestrator
```

## Development

```bash
# In orchestrator/ directory (or use docker):
docker run --rm -v $(pwd)/orchestrator:/app -w /app python:3.12 bash -c "
  pip install -e '.[dev]' &&
  ruff check rorch/ tests/ &&
  pyright rorch/ &&
  pytest -v --cov=rorch
"
```

## Architecture (SOLID)

- **Single Responsibility**: Each module has one concern (config, GitHub API, Docker CLI, scaling logic)
- **Open/Closed**: New clients can be added without changing the scaler
- **Dependency Inversion**: `PoolScaler` depends on `RunnerAPIClient` and `ContainerManager` protocols, not concrete classes
- **Interface Segregation**: Protocols define minimal method sets

Flow: `__main__.py` → creates `GitHubClient` + `DockerClient` → injects into `PoolScaler` → calls `tick()` per pool per interval

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

## What NOT to do

- Don't commit `.env` or `config.yml` (they contain PATs)
- Don't use the Docker Python SDK — the project uses CLI deliberately for simplicity
- Don't make runners non-ephemeral — the scaling logic depends on ephemeral behavior
- Don't change container naming format without updating the prefix-based filtering
- Don't add direct dependencies between `github_client.py` and `docker_client.py` — they communicate through `scaler.py`
