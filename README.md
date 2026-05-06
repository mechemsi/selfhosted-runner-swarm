# RORCH — GitHub Actions Runner Orchestrator

Self-hosted GitHub Actions runner orchestrator that dynamically scales ephemeral runner containers based on job queue demand.

## What it does

RORCH watches your GitHub Actions job queues and automatically spins up/down Docker containers running the official GitHub Actions runner. Each runner is ephemeral — it picks up one job, executes it, and exits.

**Key features:**

- **Multi-pool support** — manage runners for different orgs, repos, or accounts from a single orchestrator
- **Auto-scaling** — spawns runners when jobs queue up, cleans up when idle
- **Resource limits** — per-runner memory and CPU caps to protect your host
- **Org or repo level** — serve all repos in an org, or dedicate runners to a single repo
- **Docker-in-Docker** — runners can spin up containers inside workflows (via host socket)
- **Ephemeral runners** — no stale state, clean environment every run
- **Stuck detection** — kills containers that fail to register within 3 minutes

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
# 1. Clone
git clone https://github.com/your-org/rorch.git
cd rorch

# 2. Build the runner image
docker build -t gh-runner:latest ./runner-image

# 3. Configure
cp .env.example .env        # add your GITHUB_PAT
cp example.config.yml config.yml  # define your pools

# 4. Run
docker-compose up -d

# 5. Watch logs
docker-compose logs -f orchestrator
```

## Configuration

### Environment variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_PAT` | — | Primary GitHub PAT (required) |
| `POLL_INTERVAL` | `15` | Seconds between queue checks |

Additional PATs can be defined for pools serving different accounts.

### Pool configuration (`config.yml`)

```yaml
defaults:
  runner_image: gh-runner:latest
  runner_labels: self-hosted,linux,x64,docker
  max_runners: 3
  min_idle: 1
  memory_limit: 10g
  cpu_limit: 0          # 0 = unlimited

pools:
  - name: my-org
    owner: my-org       # GitHub org or user
    # repo: my-repo     # omit for org-level (all repos)
    pat: "${GITHUB_PAT}"
    max_runners: 10
    min_idle: 2
```

**Pool types:**

| Type | Config | Serves |
|------|--------|--------|
| Org-level | `owner` only | All repos in the org |
| Repo-level | `owner` + `repo` | Single repository |

See [`example.config.yml`](example.config.yml) for detailed examples with comments.

### Resource limits

| Setting | Description |
|---------|-------------|
| `memory_limit` | Hard memory cap per runner (e.g., `10g`, `512m`) |
| `cpu_limit` | CPU cores per runner (`0` = unlimited) |

All runners also get `--pids-limit 512` to prevent fork bombs.

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
- Default timeout is 3 minutes for registration
- Slow networks or rate-limited APIs can cause this
- Check orchestrator logs for "stuck" messages

**Docker socket errors:**
- Ensure Docker socket is at `/var/run/docker.sock`
- Runner containers need the `runner` user in the docker group (GID 991 by default)
- Adjust GID in `runner-image/Dockerfile` if your host uses a different docker GID

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
