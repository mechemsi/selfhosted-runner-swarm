---
title: Peer federation - equal orchestrators sharing a queue by free capacity
status: active
date: 2026-11-06
related: [../decisions/002-peer-federation-split-not-leader.md]
---

# Peer federation

## Problem

Two rorch hosts (runner-orch and runner-pve3) serve the same pools and labels. Today each
scales on its own view of the queue, so the second host only stays out of the way through
`queued_min_age` (90 s on pve3): it is an overflow host that reacts late, and when both react
they both spawn for the same jobs. Neither host knows how loaded or how hot the other is.

## Goal

Equal peers. Each queued job is covered by exactly one host when both are healthy, the host
with more free CPU takes more, a host at or above `max_temp_c` (default 85 C) takes no new
runners, and one dashboard shows both hosts. When the peer cannot be reached, a host behaves
exactly as it does today.

## Design

1. **Host metrics** (`rorch/hostmetrics.py`): cores from `sched_getaffinity`, `cpu_count`,
   `load1` from `/proc/loadavg`, `MemTotal`/`MemAvailable` from `/proc/meminfo`, CPU
   temperature from hwmon (`k10temp` Tctl, `coretemp` Package id 0, `zenpower` Tdie). No sensor
   means `temp_c: null`, shown as "unknown", never as cool. `free_cores =
   floor(cores * max(0, 1 - load1 / cpu_count))`. Load and memory are host-wide inside an LXC
   (no lxcfs), which is what we want.
2. **Snapshot**: every main-loop tick the orchestrator builds one snapshot: instance, version,
   timestamp, host metrics, `hot`, global free slots, and per non-personal pool `running`,
   `busy`, `max_runners` from the pool's last inspection. The same object is served to peers
   and used for this host's own share, so both hosts reason about the same numbers.
3. **Peer listener**: a second, read-only waitress app on `RORCH_PEER_PORT` (8081) with one
   route, `GET /peer/state`, behind `Authorization: Bearer $RORCH_PEER_TOKEN`. It starts only
   when the token is set. The control API on 8080 stays unpublished (it can start root
   containers). One shared token for both directions.
4. **Config**, top level of `config.yml`:

   ```yaml
   federation:
     enabled: false          # scaling flag; peers are fetched for the dashboard either way
     max_temp_c: 85
     rescue_after_seconds: 180
     stale_after_seconds: 60
     timeout_seconds: 2
     peers:
       - name: pve3
         url: http://192.168.1.245:8081
   ```

5. **Split** (`rorch/federation.py`, pure): for one pool, hosts = this host plus every peer whose
   snapshot is fresh and has the pool. Capacity per host = `min(max_runners - busy,
   global_free + running - busy)`, 0 when hot. Each queued job goes, one at a time, to the
   host with capacity left that maximises `(free_cores + 1) / (assigned + 1)`, ties by name.
   The sum of shares is `min(queued, total capacity)` and no share exceeds its capacity.
6. **Scaler**: for a federated non-personal pool the queue is scanned once for job ages
   (`get_queued_ages`), ignoring `queued_min_age`. This host's queued input becomes
   `max(own share, jobs older than rescue_after_seconds)` and then today's formula runs
   unchanged: `desired = min(max, max(min_idle, busy + queued))`. Own not-yet-busy runners
   cover own share, so no stale peer data is needed for that. A hot host spawns nothing.
   The rescue term means a job no host picked (shares computed from slightly different
   snapshots) is covered by both after 180 s: the worst case is today's behaviour.
7. **Fallback**: flag off, no fresh peer, or personal pool means today's path. One exception on
   purpose: with the flag on, a hot host starts no runners even when its peer is down, because
   the temperature limit protects the hardware.
8. **Dashboard**: `/api/state` gains `host` and `federation` (enabled, peers with their last
   snapshot, age and error). The peers are fetched server-side by the orchestrator, so the
   dashboard needs no new route and no token reaches the browser. A Hosts panel shows each
   host: cores, free cores, load, memory, temperature (or "unknown"), hot, per-pool
   running/busy, reachability.

## Steps (each shippable)

1. This plan and ADR 002.
2. Host metrics in `/api/state` plus tests (temperature parsing, absent sensor).
3. Federation config and validation, peer listener, fetch with timeout and staleness, pure
   split, scaler integration behind `enabled: false`. Property-style split tests, peer timeout
   tests, config validation tests.
4. Dashboard Hosts panel. Browser check at 390 and 1280 px.
5. Deploy both hosts with the flag off, compare, then turn it on, then watch a real burst.

## Rollback

`federation.enabled: false` plus an orchestrator restart. Removing the block entirely is the
pre-federation behaviour. The image itself rolls back with `RORCH_VERSION=<previous>`.

## Not done

- No remote control of a peer (pause, spawn) from the dashboard. Read-only by design.
- No more than one token; no mTLS. LAN only.
- No leader election. See ADR 002.
