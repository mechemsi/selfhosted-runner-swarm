# Testing Rules

## Orchestrator (Python)
- Test config parsing with valid and invalid YAML
- Test scaling formula: `desired = min(max, max(min_idle, busy + queued))`
- Test container name generation and prefix filtering
- Test stuck container timeout parsing
- Mock GitHub API responses and Docker CLI output for unit tests
- Use pytest if adding a test suite

## Runner image
- Test entrypoint.sh by running with missing env vars (should fail gracefully)
- Verify Dockerfile builds without errors: `docker build -t gh-runner:test ./runner-image`

## Integration
- Test full flow: orchestrator sees queued job -> spawns container -> container registers
- Use `docker-compose up --build` after changes
- Check logs for errors: `docker-compose logs -f orchestrator`

## Before committing
- Verify orchestrator syntax: `python3 -m py_compile orchestrator/orchestrator.py`
- Verify shell scripts: `shellcheck runner-image/entrypoint.sh` (if shellcheck available)
- Verify Docker builds: `docker build -t gh-runner:test ./runner-image`
