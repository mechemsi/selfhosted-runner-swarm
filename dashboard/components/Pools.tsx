// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

"use client";

import { useState } from "react";
import type { Pool, Role } from "./types";
import { ago } from "./ago";

interface Props {
  pools: Pool[];
  role: Role;
  act: (path: string, body?: unknown) => Promise<Response>;
  refresh: () => Promise<void>;
}

export default function Pools({ pools, role, act, refresh }: Props) {
  const admin = role === "admin";
  const [edits, setEdits] = useState<Record<string, { max?: string; min?: string }>>({});

  async function save(pool: Pool) {
    const edit = edits[pool.config.name] ?? {};
    const body: Record<string, string> = {};
    if (edit.max !== undefined) body.max_runners = edit.max;
    if (edit.min !== undefined) body.min_idle = edit.min;
    if (!Object.keys(body).length) return;
    const res = await fetch(`/api/rorch/api/config/pools/${pool.config.name}`, {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) alert(((await res.json()) as { error?: string }).error ?? res.statusText);
    setEdits((e) => ({ ...e, [pool.config.name]: {} }));
    await refresh();
  }

  async function reset(pool: Pool) {
    if (!confirm(`Revert ${pool.config.name} to config.yml?`)) return;
    const res = await fetch(`/api/rorch/api/config/pools/${pool.config.name}/overrides`, {
      method: "DELETE",
      credentials: "same-origin",
    });
    if (!res.ok) alert(((await res.json()) as { error?: string }).error ?? res.statusText);
    await refresh();
  }

  return (
    <section>
      <h2>Pools</h2>
      <div className="wrap">
        <table>
          <thead>
            <tr>
              <th>pool</th><th>target</th><th>state</th><th>containers</th><th>online</th>
              <th>idle</th><th>busy</th><th>queued</th><th>max</th><th>min idle</th>
              <th>tick</th>{admin ? <th>actions</th> : null}
            </tr>
          </thead>
          <tbody>
            {pools.map((p) => {
              const name = p.config.name;
              const edit = edits[name] ?? {};
              return (
                <>
                  <tr key={name}>
                    <td><strong>{name}</strong></td>
                    <td className="muted">{p.config.display}</td>
                    <td>
                      {p.paused ? <span className="bad">paused</span>
                        : p.draining ? <span className="warn">draining</span>
                        : <span className="ok">active</span>}
                    </td>
                    <td>{p.containers}</td><td>{p.online}</td><td>{p.idle}</td><td>{p.busy}</td>
                    <td>{p.queued > 0 ? <span className="warn">{p.queued}</span> : p.queued}</td>
                    <td>
                      {admin ? (
                        <input
                          value={edit.max ?? String(p.config.max_runners)}
                          onChange={(e) =>
                            setEdits((s) => ({ ...s, [name]: { ...edit, max: e.target.value } }))}
                        />
                      ) : p.config.max_runners}
                    </td>
                    <td>
                      {admin ? (
                        <input
                          value={edit.min ?? String(p.config.min_idle)}
                          onChange={(e) =>
                            setEdits((s) => ({ ...s, [name]: { ...edit, min: e.target.value } }))}
                        />
                      ) : p.config.min_idle}
                    </td>
                    <td className="muted">
                      {p.duration ? `${p.duration.toFixed(2)}s` : "—"} · {ago(p.last_tick)}
                    </td>
                    {admin ? (
                      <td className="row">
                        <button onClick={() => void act(`/api/pools/${name}/state`, { paused: !p.paused })}>
                          {p.paused ? "resume" : "pause"}
                        </button>
                        <button onClick={() => void act(`/api/pools/${name}/state`, { draining: !p.draining })}>
                          {p.draining ? "undrain" : "drain"}
                        </button>
                        <button onClick={() => void act(`/api/pools/${name}/scale`, { delta: 1 })}>+1</button>
                        <button onClick={() => void act(`/api/pools/${name}/scale`, { delta: -1 })}>−1</button>
                        <button onClick={() => void save(p)}>save</button>
                        <button onClick={() => void reset(p)}>reset</button>
                      </td>
                    ) : null}
                  </tr>
                  {p.repos.map((r) => (
                    <tr key={`${name}-${r.name}`}>
                      <td />
                      <td className="muted">↳ {r.name.replace(`${name}-`, "")}</td>
                      <td />
                      <td>{r.containers}</td><td>{r.online}</td><td>{r.idle}</td><td>{r.busy}</td>
                      <td>{r.queued > 0 ? <span className="warn">{r.queued}</span> : r.queued}</td>
                      <td /><td /><td />{admin ? <td /> : null}
                    </tr>
                  ))}
                  {p.repo_count ? (
                    <tr key={`${name}-note`}>
                      <td className="muted" colSpan={admin ? 12 : 11}>
                        {p.repo_count} repositories tracked
                        {p.repos.length ? `, ${p.repos.length} active` : ", all quiet"}
                      </td>
                    </tr>
                  ) : null}
                </>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
