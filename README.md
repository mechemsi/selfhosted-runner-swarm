# RORCH — GitHub Actions Runner Orchestrator

Self-hosted GitHub Actions runner orchestrator that dynamically scales ephemeral runner containers based on job queue demand.

## What it does

RORCH watches your GitHub Actions job queues and automatically spins up/down Docker containers running the official GitHub Actions runner. Each runner is ephemeral — it picks up one job, executes it, and exits.

**Key features:**

- **Multi-pool support** — manage runners for different orgs, repos, or accounts from a single orchestrator
- **Auto-scaling** — spawns runners when jobs queue up, cleans up when idle
- **Resource limits** — per-runner memory and CPU caps to protect your host
- **Org, personal, or repo level** — discover personal repos automatically or target a fixed scope
- **Docker-in-Docker** — runners can spin up containers inside workflows (via host socket)
- **Ephemeral runners** — no stale state, clean environment every run
- **Stuck detection**: kills containers that fail to register within `stuck_timeout` (8 minutes), never one running a job

## Architecture

```
┌─────────────────────┐
│   Orchestrator      │     polls GitHub API
│   (Python 3.12)     │◄──── every N seconds
│                     │
│  ┌──────┐ ┌──────┐  │     spawns/kills containers
│  │Pool 1│ │Pool 2│  │────► via Docker CLI
│  └──────┘ └──────┘  │
└─────────────────────┘
         │
    Docker socket
         │
┌────────┴────────┐
│  Runner containers  │   ephemeral, one job each
│  (Ubuntu 22.04)     │   auto-register + deregister
└─────────────────────┘
```

## Prerequisites

- Docker Engine with Docker Compose
- GitHub Personal Access Token (PAT) with:
  - **Actions**: read/write
  - **Administration**: read/write

## Quick start

```bash
git clone https://github.com/your-org/rorch.git
cd rorch

make setup      # scaffolds .env + config.yml, builds the runner image for this host
# edit .env (GITHUB_PAT) and config.yml (your pools)
make up         # start — prints the dashboard URL
make logs       # follow the orchestrator
```

`make help` lists every target. The main ones:

| Target | Does |
|--------|------|
| `make setup` | First-time setup: scaffold config, generate a dashboard token, build the runner image |
| `make up` / `make down` / `make restart` | Compose lifecycle |
| `make logs` / `make ps` | Follow logs · list orchestrator and runner containers |
| `make rebuild` | Rebuild the runner image and recreate the orchestrator |
| `make update RUNNER_VERSION=x.y.z` | Move to a different runner agent and recreate |
| `make dashboard` | Print the dashboard URL including the auth token |
| `make check` | Everything CI runs: ruff, pyright, pytest |
| `make build-images` | Validate both Dockerfiles build |

<details>
<summary>Manual equivalent, without make</summary>

```bash
./scripts/build-runner.sh
cp .env.example .env               # add your GITHUB_PAT
cp example.config.yml config.yml   # define your pools
docker compose up -d
docker compose logs -f orchestrator
```
</details>

## Dashboard

The orchestrator serves a web dashboard and JSON API on `127.0.0.1:8080`.

```bash
make dashboard      # prints http://127.0.0.1:8080/?token=…
```

It shows every pool (containers, online, idle, busy, queued, last tick), every runner
container with what GitHub reports for it, the global cap and GitHub rate budget,
lifecycle events, and history charts. From it you can stop or restart a runner, pause /
resume / drain a pool, nudge a pool ±1, pause all provisioning, and edit pool limits —
all applied on the next tick with no restart.

> **⚠️ This port is root-equivalent on the host.** The orchestrator mounts
> `/var/run/docker.sock`, so anything that can reach the API can start privileged
> containers. `docker-compose.yml` publishes it on `127.0.0.1` only, the server refuses
> to bind a non-loopback address without `RORCH_API_TOKEN`, and every mutating request is
> recorded in an audit log. Put a real reverse proxy with its own auth in front before
> exposing it beyond the host.

### API

Authenticate with `Authorization: Bearer $RORCH_API_TOKEN`.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/state` | Pools, containers, globals, rate budget, recent events |
| `GET /api/history?hours=6` | Snapshot series and event counts for charts |
| `GET /api/events` · `GET /api/audit` | Lifecycle events · who changed what |
| `GET /metrics` | Prometheus text exposition (no client library needed) |
| `POST /api/containers/{name}/stop\|restart` | Stop a runner (`confirm: true` required if busy) |
| `POST /api/containers/{name}/protect` | Exempt a runner from the lifetime reaper |
| `POST /api/pools/{name}/state` | `{paused, draining}` |
| `POST /api/pools/{name}/scale` | `{delta: 1 \| -1}` |
| `POST /api/pause` | Pause all provisioning |
| `GET /api/config` · `PATCH /api/config/pools/{name}` | Read · override pool settings |
| `DELETE /api/config/pools/{name}/overrides` | Revert a pool to `config.yml` |
| `POST /api/config/pools` · `DELETE /api/config/pools/{name}` | Add · remove a pool |
| `PATCH /api/config/globals` | `max_total_runners`, `max_runner_lifetime`, `paused` |
| `GET /api/config/export` | Effective config as YAML, to copy back into `config.yml` |

Send `Idempotency-Key: <uuid>` on any POST; a retry with the same key replays the first
response instead of provisioning a second runner.

**Token scopes.** `RORCH_API_TOKEN` grants everything. `RORCH_API_READONLY_TOKEN` grants
every `GET` but returns `403` on any control action or config change, so a dashboard can be
shared without handing over container control. After 10 bad tokens a client IP is locked out
for 5 minutes with `429` + `Retry-After` — the token is the only credential, so unlimited
guessing would be the whole attack.

PATs are never stored in the database and never appear in any response.

### config.yml stays the default

Settings changed from the dashboard are stored as an **overlay** in
`/app/data/rorch.db` (the `rorch-data` volume). `config.yml` remains the baseline:

- No override for a field → the `config.yml` value is used.
- **Reset** in the UI (or `DELETE …/overrides`) drops the override row.
- Deleting the database file reverts the orchestrator to exactly its `config.yml` behaviour.
- Running with `RORCH_DB=off` disables the store and dashboard entirely.

Pools created through the API name an environment variable for their PAT (`pat_env`), so
secrets stay in `.env`.

## Configuration

### Environment variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_PAT` | — | Primary GitHub PAT (required) |
| `POLL_INTERVAL` | `15` | Main loop wake-up interval |
| `POOL_TICK_WORKERS` | one per pool, at least `8` | Pools that can tick at the same time (1-64) |
| `GITHUB_MAX_CONCURRENT_REQUESTS` | `8` | GitHub requests in flight at once, across all pools (1-32) |
| `REPO_DISCOVERY_TTL` | `600` | Personal repository-list cache lifetime in seconds |
| `GITHUB_POLL_INTERVAL` | `60` | Minimum seconds between GitHub scans for each pool |
| `GITHUB_RATE_LIMIT_RESERVE` | `100` | Stop before consuming the final PAT requests |
| `REPO_CHECK_WORKERS` | `6` | Maximum repositories inspected concurrently |
| `RUNNER_OPERATION_WORKERS` | `4` | Maximum concurrent runner removals and provisions |
| `RORCH_API_TOKEN` | generated | Dashboard/API control token. If unset while bound off-loopback, one is generated **and stored**, so it survives restarts |
| `RORCH_API_READONLY_TOKEN` | — | Optional token: every read endpoint, no control actions |
| `RORCH_API_PORT` | `8080` | Dashboard port, published on `127.0.0.1` |
| `HISTORY_RETENTION_DAYS` | `14` | Days of dashboard history kept (`0` disables pruning) |
| `RORCH_DB` | — | Set to `off` to run without the store and dashboard |
| `RUNNER_NETWORK_MODE` | `host` | Runner network namespace — `host` or `bridge` |
| `RUNNER_WORK_TMPFS_SIZE` | `auto` | tmpfs size for the runner work dir (`0` = off) |
| `STUCK_TIMEOUT` | `8` | Minutes a runner may take to come online before it is killed as stuck |
| `CI_CONTAINER_MAX_AGE` | `360` | Minutes before leftover CI job containers are removed (`0` = off) |
| `CI_CONTAINER_PATTERNS` | none | Comma-separated name globs or `label=KEY` marking extra CI containers |

Additional PATs can be defined for pools serving different accounts.

### Pool configuration (`config.yml`)

```yaml
defaults:
  runner_image: gh-runner:latest
  runner_labels: self-hosted,linux,x64,docker
  max_runners: 3
  min_idle: 1
  github_poll_interval: 60
  repo_check_workers: 6
  runner_operation_workers: 4
  memory_limit: 10g
  cpu_limit: 0          # 0 = unlimited
  work_tmpfs_size: auto # half of memory_limit; 0 = off

pools:
  - name: my-org
    owner: my-org
    # repo: my-repo     # omit for org-level (all repos)
    pat: "${GITHUB_PAT}"
    max_runners: 10
    min_idle: 2
```

**Pool types:**

| Type | Config | Serves |
|------|--------|--------|
| Org-level | `owner` only | All repos in the org |
| Personal all-repos | `owner` + `scope: personal` | All accessible repos owned by the personal account |
| Repo-level | `owner` + `repo` | Single repository |

Personal pools cache repository discovery for 10 minutes by default while continuing to poll
known repositories every `github_poll_interval`. Set `repo_discovery_ttl` per pool, or set it to `0`
to disable caching. If a refresh fails, RORCH continues with the last successful list. The cache
is in memory and starts empty after a restart. GitHub runners remain repository-scoped, but
RORCH chooses the repository automatically and applies `max_runners` across the whole pool.
Because an idle runner cannot serve arbitrary personal repositories, personal pools default
`min_idle` to `0`.

Repository checks are I/O-bound and run concurrently through a bounded worker pool. Runner
state is fetched once per repository and reused for cleanup and scaling. After all checks finish,
RORCH makes one account-wide capacity decision, then removes stale runners and provisions new
ones through a separate bounded worker pool. This preserves `max_runners` while avoiding serial
waits across large personal accounts. Set `repo_check_workers` and `runner_operation_workers`
per pool to tune concurrency; limits are 32 and 16 respectively.

### Pools tick independently

Each pool ticks on its own lane. The main loop wakes every `POLL_INTERVAL`, starts a tick for
every pool whose previous tick has finished, and goes back to sleep without waiting for any of
them, so a pool whose GitHub scan takes 40 seconds delays only itself. A pool is never ticked
twice at once: if its last tick is still running it is skipped that round (and logged as stalled
after 10 minutes). Aged-runner cleanup, network pruning and periodic housekeeping run on a
separate executor, so busy pools cannot starve them.

`max_total_runners` still holds while pools spawn concurrently: counting the running containers
and granting spawn slots is one atomic step, and granted slots stay reserved until their
`docker run` returns, so two pools deciding at the same moment cannot both claim the last slot.

### GitHub API rate budget

RORCH limits each pool to one GitHub scan per `github_poll_interval` (60 seconds by default),
even when the main loop wakes every 15 seconds. Authenticated GET responses are cached with
their `ETag`; unchanged `304 Not Modified` responses do not consume GitHub's primary rate limit.
At most `GITHUB_MAX_CONCURRENT_REQUESTS` GitHub requests are in flight at once (well under
GitHub's documented limit of 100 concurrent requests), and mutating requests (runner
deregistration) stay strictly serial, at least one second apart, as GitHub recommends. A repo
check costs three requests when nothing is running (runners, queued runs, in-progress runs) plus
one jobs request per active run.

Every response updates the token's remaining/reset budget. RORCH stops when
`GITHUB_RATE_LIMIT_RESERVE` requests remain, resumes after `X-RateLimit-Reset`, and honors
`Retry-After` for secondary limits. During a cooldown, scaling is skipped rather than interpreting
failed API calls as zero queued jobs. Conditional-response caching is in memory, bounded to 2,048
entries, and cleared on restart.

See [`example.config.yml`](example.config.yml) for detailed examples with comments.

### Resource limits

| Setting | Description |
|---------|-------------|
| `memory_limit` | Hard memory cap per runner (e.g., `10g`, `512m`) |
| `cpu_limit` | CPU cores per runner (`0` = unlimited) |
| `work_tmpfs_size` | tmpfs over the runner work dir, `/home/runner/actions-runner/_work` (`auto` = half of `memory_limit`, a size such as `4g`, or `0` = off) |

All runners also get `--pids-limit 512` to prevent fork bombs.

The work dir holds checkouts, `node_modules` and build output, and is rewritten by every job.
Without the tmpfs it lands in the container's writable layer, i.e. on the host disk. tmpfs pages
are charged to the runner's memory cgroup, so the workspace and the job's processes share
`memory_limit`: a job that fills its workspace past that is OOM-killed, one that fills a fixed
`work_tmpfs_size` gets "No space left on device". RORCH warns at startup if `work_tmpfs_size` is
not below `memory_limit`. The work dir was never bind-mounted from the host, so jobs could not
pass `$GITHUB_WORKSPACE` paths to sibling containers before either; the tmpfs changes nothing there.

### Leftover CI containers

A job's `services:` and any `docker run` in its steps go through the host Docker socket, so they
are siblings of the runner rather than children. When a job is cancelled the runner container
dies before its cleanup step and those siblings keep running. Every ~15 minutes RORCH removes
containers older than `ci_container_max_age` minutes (default `360`, GitHub's default job
timeout; `0` disables) that are either:

- on a `github_network_*` network, i.e. a runner-created `services:` or container-job container, or
- matched by a `ci_container_patterns` entry: a name glob (`my-app-ci-*`) or `label=KEY`.

`gh-runner-*` and `rorch-*` containers are never removed, whatever the patterns say.

```yaml
ci_container_max_age: 360
ci_container_patterns:
  - my-app-ci-*
  - label=my-app-ci
```

### Runner agent versions

The agent version is baked into the image at build time. Images are tagged by version, and
`gh-runner:latest` follows the newest:

```bash
./scripts/build-runner.sh                          # newest → gh-runner:2.337.0 + gh-runner:latest
RUNNER_VERSION=2.328.0 ./scripts/build-runner.sh   # older  → gh-runner:2.328.0 only
```

Pools default to `gh-runner:latest`. A pool needing an older agent pins it:

```yaml
- name: legacy-ci
  owner: acme
  runner_image: gh-runner:2.328.0   # this pool only
```

If a pinned image is missing, the orchestrator's auto-build reads the version back out of the
tag and passes it to the build, so a pinned pool never silently gets a different agent.

> **The agent must be ≥ 2.327** for actions on the Node24 runtime (current `actions/*`,
> `shivammathur/setup-php@v2`). Older agents fail with
> `'using: node24' is not supported`. CI asserts this on every image build, and a weekly
> job opens an issue when upstream moves ahead of the pinned version.

### Which repositories a pool covers

`scope: personal` and org-level pools discover repositories automatically. Two settings
control what they pick up:

```yaml
- name: personal
  owner: you
  scope: personal
  include_public_repos: false     # default — public repos are skipped
  exclude_repos:                  # list form
    - scratch
    - legacy-*
  # exclude_repos: scratch, legacy-*   # or comma-separated, same thing
```

> **Public repositories are skipped by default, and should stay that way.** A fork PR
> carries its own workflow file and chooses its own `runs-on` labels, so a self-hosted
> runner registered to a public repo lets anyone who can open a PR execute code on the
> host — the one holding `/var/run/docker.sock` and every PAT in your config. GitHub's
> own guidance is not to use self-hosted runners with public repositories. A
> fork-approval policy helps but is not a boundary: it stops first-time contributors,
> not anyone with a merged PR already.

Skipped and excluded repositories are named in the startup log, so a pool that provisions
nothing is diagnosable rather than mysterious. `exclude_repos` is editable from the
dashboard; `include_public_repos` deliberately is not — it is a posture decision that
belongs in `config.yml`.

### Runner networking and host port collisions

Runners use the host network namespace by default. That means a job's `services:` containers
bind **host** ports, so a workflow mapping `5432:5432` fails with
`Bind for :::5432 failed: port is already allocated` if the host already runs Postgres.

Three ways out, cheapest first:

1. **Map around it in the workflow** — give the service a free host port
   (`ports: ["0:5432"]` and read the assigned port), which is what most repos should do.
2. **Isolate the pool** — `network_mode: bridge` gives its runners their own namespace, so
   service containers no longer touch host ports. The trade-off: jobs can no longer reach
   host services over `localhost`.
3. **Dedicate a host** to that pool.

```yaml
- name: needs-services
  owner: acme
  network_mode: bridge      # default is "host"
```

## Scaling logic

```
desired_runners = min(max_runners, max(min_idle, busy + queued))
```

- Always keeps at least `min_idle` runners warm
- Spawns enough runners to cover all queued jobs
- Never exceeds `max_runners`
- Runners auto-deregister after completing one job

## Project structure

```
rorch/
├── docker-compose.yml       # Orchestrator service definition
├── .env.example             # Environment template
├── example.config.yml       # Pool config with examples
├── orchestrator/
│   ├── Dockerfile           # Orchestrator image (python:3.12-slim)
│   └── orchestrator.py      # Main application (~500 lines)
└── runner-image/
    ├── Dockerfile           # Runner image (ubuntu:22.04 + actions-runner)
    └── entrypoint.sh        # Registration and lifecycle script
```

## Troubleshooting

**Runners not registering:**
- Verify PAT permissions (Actions + Administration, read/write)
- Check if the PAT has access to the target org/repo
- Look at runner container logs: `docker logs gh-runner-<pool>-<id>`

**Runners killed as stuck:**
- A runner container that GitHub does not list as online or busy after `stuck_timeout` minutes
  (default 8) is killed, unless a `Runner.Worker` process is alive in it (checked with
  `docker top`; the listener only forks a worker for an accepted job). Under heavy load GitHub
  can report working runners offline, so GitHub's flags alone are not trusted to kill.
- If Docker cannot list a container's processes, it is spared for that tick;
  `max_runner_lifetime` stays the backstop.
- Slow registration (busy host, slow network, rate-limited APIs) needs a higher `stuck_timeout`,
  per pool or under `defaults`
- Check orchestrator logs for "Stuck" (killed) and "sparing it" (protected) messages

**GitHub API rate limited:**
- RORCH pauses automatically until GitHub's reset or retry time
- Increase `github_poll_interval` for accounts with many repositories
- Keep `GITHUB_RATE_LIMIT_RESERVE` above zero when a PAT is shared with other tools

**Docker socket errors:**
- Ensure Docker socket is at `/var/run/docker.sock`
- The runner image must be built with the host's docker group GID. `scripts/build-runner.sh` detects this automatically via `getent group docker`.
- If you ran `docker build` directly, pass it explicitly: `docker build --build-arg DOCKER_GID=$(getent group docker | cut -d: -f3) -t gh-runner:latest ./runner-image`
- Symptom of GID mismatch: runners exit immediately with `Cannot connect to Docker socket at /var/run/docker.sock` in the container logs.

## Auto-start on server boot

The orchestrator container has `restart: unless-stopped`, so once started it will survive Docker daemon restarts. To bring the compose project up automatically after a host reboot, install a systemd unit.

Create `/etc/systemd/system/rorch.service` (adjust `WorkingDirectory` to your checkout path):

```ini
[Unit]
Description=RORCH orchestrator
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/domas/pr/rorch
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rorch.service
sudo systemctl status rorch.service
```

After a reboot, `docker compose ps` should show the orchestrator running without manual intervention.

## License

MIT
