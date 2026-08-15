<!-- DO NOT EDIT — generated from ~/brain on 2026-06-17. Edit brain/projects/rorch/ instead. -->

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
│   │   ├── scaler.py            # Scaling logic (PoolScaler)
│   │   ├── store.py             # SQLite: history, overrides, control state, audit
│   │   ├── resolver.py          # config.yml + DB overrides -> EffectiveConfig
│   │   ├── server.py            # Flask dashboard/API in a daemon thread
│   │   └── dashboard.html       # Single-page UI (no build step)
│   └── tests/
│       ├── conftest.py          # Shared fixtures
│       ├── test_config.py
│       ├── test_docker_client.py
│       ├── test_scaler.py
│       ├── test_store.py
│       ├── test_resolver.py
│       └── test_server.py
├── runner-image/
│   ├── Dockerfile
│   └── entrypoint.sh
├── .github/workflows/ci.yml    # CI: ruff, pyright, pytest
├── docker-compose.yml
├── example.config.yml
└── .env.example
```

## Running the project

Everything runs in Docker. Use the Makefile — it is the one entrypoint.

```bash
make setup     # scaffold .env + config.yml, build the runner image for this host's docker GID
make up        # start (prints the dashboard URL)
make logs
```

## Development

```bash
make check         # ruff + ruff format --check + pyright + pytest, all inside docker
make fmt           # apply ruff fixes and formatting
make build-images  # validate both Dockerfiles build
```

Raw equivalent if you need it:

```bash
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

Flow: `__main__.py` → opens `SqliteStore` → creates `GitHubClient` + `DockerClient` → injects into `PoolScaler` → starts the Flask API thread → each interval calls `ConfigResolver.resolve()` then `tick()` per pool

### Dashboard layering

- `config.yml` is always the baseline. `store.py` holds an **overlay**; `resolver.py` merges them into an `EffectiveConfig` rebuilt every tick.
- **An empty (or absent) database must behave exactly like the pre-dashboard orchestrator.** Every store call site treats `store is None` as "carry on", and store failures are logged at debug level rather than breaking a tick. Keep it that way.
- PATs never enter the database. Pools created through the API store `pat_env` (an environment variable name) and resolve the secret at tick time.
- The API port is root-equivalent (Docker socket). Loopback-only by default; `server.start()` refuses a non-loopback bind without a token.

## Key formulas

```
desired = min(max_runners, max(min_idle, busy + queued))
```

## Important constraints

- `config.yml` and `.env` are gitignored — they contain secrets
- Runner image must be built before starting the orchestrator
- Docker socket must be mounted for the orchestrator to manage containers
- GitHub PAT needs Actions (read/write) + Administration (read/write) permissions
- Runner image must be built with `--build-arg DOCKER_GID=$(stat -c %g /var/run/docker.sock)`, else the runner can't read the socket and exits before registering (the orchestrator's auto-build does this and stamps the GID as a `rorch.docker_gid` label)

## Before committing changes to runner-image/

Docker builds **are** covered in CI now:

- `docker-lint` runs `docker buildx build --check` plus hadolint on both Dockerfiles on every PR touching them.
- `docker-build-scan` builds both images for real, gated on `lint` + `test-unit`, and asserts the runner agent is ≥ 2.327 (Node24 support) and that the entrypoint is executable. It is off by default (~16 min); enable with the repository variable `DOCKER_BUILD_CHECKS=true`.

**CI must never run on this project's own self-hosted runners.** The repository is public, so a fork PR chooses its own `runs-on` labels — a self-hosted label would let any contributor execute code on the host holding the Docker socket and the GitHub PATs. The `repo-rules` job fails the build if a workflow asks for one. rorch must likewise not be configured with a pool for a public repository.

Still verify locally before pushing — the runner image build is slow and its failures are
easier to read on your own machine:
```bash
make build-images
```

## CI

`.ci-profile.yml` records the declared signals (no deploy target, not consumed as a
dependency, GitHub Actions, hosted-by-default runners) and, importantly, the detection rows
that deliberately do **not** apply. Read it before adding a job "for completeness".

`repo-rules` in CI enforces the "What NOT to do" list below by grep — if you change one of
those rules here, update that job too.

## What NOT to do

- Don't commit `.env` or `config.yml` (they contain PATs)
- Don't use the Docker Python SDK — the project uses CLI deliberately for simplicity
- Don't make runners non-ephemeral — the scaling logic depends on ephemeral behavior
- Don't change container naming format without updating the prefix-based filtering
- Don't add direct dependencies between `github_client.py` and `docker_client.py` — they communicate through `scaler.py`
- Don't let the dashboard become required: no store must still mean a working orchestrator
- Don't write a PAT into the database or return one from the API
