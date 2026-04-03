# /deploy — Pre-deployment Checklist

Run through deployment verification before pushing changes to production.

## Steps

1. **Syntax check**: `python3 -m py_compile orchestrator/orchestrator.py`
2. **Shell check**: `shellcheck runner-image/entrypoint.sh` (if available)
3. **Build runner image**: `docker build -t gh-runner:latest ./runner-image`
4. **Build orchestrator**: `docker compose build orchestrator`
5. **Config validation**: Verify `example.config.yml` parses correctly
6. **Review changes**: `git diff` — check for accidental secret exposure
7. **Check .gitignore**: Ensure `.env` and `config.yml` are listed
8. **Deploy**: `docker compose up -d` and verify with `docker compose logs -f orchestrator`
