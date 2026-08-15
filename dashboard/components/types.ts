// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

export interface PoolConfig {
  name: string;
  display: string;
  max_runners: number;
  min_idle: number;
}

export interface RepoRow {
  name: string;
  containers: number;
  online: number;
  idle: number;
  busy: number;
  queued: number;
}

export interface Pool {
  config: PoolConfig;
  paused: boolean;
  draining: boolean;
  containers: number;
  online: number;
  idle: number;
  busy: number;
  queued: number;
  duration: number;
  last_tick: number;
  repos: RepoRow[];
  repo_count: number;
}

export interface JobRef {
  repo: string;
  workflow: string;
  job_name: string;
}

export interface Container {
  name: string;
  image: string;
  status: string;
  running_for: string;
  minutes: number;
  protected: boolean;
  github: { status?: string; busy?: boolean };
  job: JobRef | null;
}

export interface Job {
  job_id: number;
  repo: string;
  workflow: string;
  job_name: string;
  runner: string;
  status: string;
  conclusion: string;
  url: string;
  ts: number;
  ended_ts: number | null;
}

export interface RunnerEvent {
  ts: number;
  pool: string;
  container: string;
  event: string;
  reason: string;
}

export interface State {
  pools: Pool[];
  containers: Container[];
  jobs: Job[];
  events: RunnerEvent[];
  globals: {
    max_total_runners: number;
    max_runner_lifetime: number;
    paused: boolean;
    total_containers: number;
  };
  rate_limit: { remaining?: number | null; blocked_until?: number };
  ts: number;
}

export type Role = "admin" | "viewer";
