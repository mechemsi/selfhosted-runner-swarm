---
title: Main-only GitHub Actions runner pools
status: completed
date: 2026-11-25
---

# Main-only GitHub Actions runner pools

- Use GitHub's workflow-restricted runner group and push-only Actions policy to enforce assignment. Verify both before requesting each just-in-time runner registration.
- Count matching main pushes from a single opted-in workflow. Keep the pool settings in YAML and reserve its label.
- Start one Docker daemon per runner without host socket or cache mounts. Put trusted pools on a dedicated host or VM because existing PR runners can control their host.
- Cover queue filtering, invalid GitHub policies and Docker launch arguments in pytest. Exercise separate daemon state with the built image.
