// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

"use client";

import { useState } from "react";
import type { Container, Role } from "./types";

interface Props {
  containers: Container[];
  role: Role;
  act: (path: string, body?: unknown) => Promise<Response>;
}

export default function Containers({ containers, role, act }: Props) {
  const admin = role === "admin";
  const [logs, setLogs] = useState<{ name: string; text: string } | null>(null);

  async function showLogs(name: string) {
    setLogs({ name, text: "loading…" });
    const res = await fetch(`/api/rorch/api/containers/${name}/logs`, {
      credentials: "same-origin",
    });
    setLogs({ name, text: res.ok ? await res.text() : `could not read logs (${res.status})` });
  }

  async function stopOrRestart(name: string, action: "stop" | "restart") {
    if (!confirm(`${action} ${name}?`)) return;
    const res = await act(`/api/containers/${name}/${action}`);
    if (res.status === 409) {
      // rorch refuses to kill a busy runner without an explicit confirm,
      // because doing so aborts a real CI job mid-flight.
      if (!confirm(`${name} is running a job. Abort it?`)) return;
      await act(`/api/containers/${name}/${action}`, {
        confirm: true,
        reason: "operator override from dashboard",
      });
    }
  }

  return (
    <>
      <section>
        <h2>Runner containers</h2>
        <div className="wrap">
          <table>
            <thead>
              <tr>
                <th>name</th><th>image</th><th>age</th><th>docker</th><th>github</th>
                {admin ? <th>actions</th> : null}
              </tr>
            </thead>
            <tbody>
              {containers.length === 0 ? (
                <tr><td className="muted" colSpan={admin ? 6 : 5}>no runner containers</td></tr>
              ) : containers.map((c) => (
                <tr key={c.name}>
                  <td>
                    {c.name}
                    {c.protected ? <span className="pill"> kept</span> : null}
                    {c.job ? (
                      <>
                        <br />
                        <span className="muted">
                          {c.job.repo} · {c.job.workflow} / {c.job.job_name}
                        </span>
                      </>
                    ) : null}
                  </td>
                  <td className="muted">{c.image}</td>
                  <td>{c.running_for}</td>
                  <td className="muted">{c.status}</td>
                  <td>
                    {c.github?.status
                      ? (c.github.busy ? <span className="warn">busy</span> : <span className="ok">idle</span>)
                      : <span className="muted">unknown</span>}
                  </td>
                  {admin ? (
                    <td className="row">
                      <button onClick={() => void showLogs(c.name)}>logs</button>
                      <button onClick={() => void act(`/api/containers/${c.name}/protect`, { protected: !c.protected })}>
                        {c.protected ? "unkeep" : "keep"}
                      </button>
                      <button onClick={() => void stopOrRestart(c.name, "restart")}>restart</button>
                      <button className="bad" onClick={() => void stopOrRestart(c.name, "stop")}>stop</button>
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {logs ? (
        <section>
          <h2>
            Logs — {logs.name} <button onClick={() => setLogs(null)}>close</button>
          </h2>
          <pre style={{ maxHeight: "22rem", overflow: "auto", whiteSpace: "pre-wrap" }}>
            {logs.text}
          </pre>
        </section>
      ) : null}
    </>
  );
}
