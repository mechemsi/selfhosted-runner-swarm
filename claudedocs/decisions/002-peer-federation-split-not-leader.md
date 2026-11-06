---
title: Peers split the queue deterministically; no leader
status: active
date: 2026-11-06
related: [../plans/2026-11-06-peer-federation.md]
---

# 002 - Peers split the queue deterministically, no leader

## Context

Two orchestrators serve the same pools. They must stop spawning twice for one job and must
share by free capacity and temperature, without a new single point of failure.

## Options

**A - Leader election** (one host decides for both). Exact, but needs a lease, failover and
a write API between hosts; a dead leader stalls both. Rejected.

**B - Shared database lock**. Puts both hosts' scaling behind one MariaDB. A LAN blip stops
CI on both. Rejected.

**C - Each host runs the same pure split over published snapshots and spawns only its own
share.** Chosen.

## Decision

C, with three guards:

- A peer that is unreachable or stale drops out of the split; with no peer left the host runs
  today's code path.
- Own share is covered by own starting runners, so a stale peer view cannot make a host spawn
  for the peer's share.
- Jobs older than `rescue_after_seconds` count on every host. Two hosts that briefly disagree
  about the split can at worst leave a job for that long, then both cover it: today's overflow
  behaviour, never worse.

## Consequences

- Small over-provision is possible when free-CPU ranks flip between ticks; it is bounded by
  the shares that moved and those runners take the next job.
- Peers are read-only to each other: one bearer token, one GET route, separate port.
