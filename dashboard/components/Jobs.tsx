// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

"use client";

import { useMemo, useState } from "react";
import type { Job } from "./types";
import { ago } from "./ago";

/** Only link out to real GitHub URLs — a javascript: href would execute. */
function safeUrl(url: string): string {
  return /^https:\/\/[\w.-]+\//.test(url) ? url : "";
}

export default function Jobs({ jobs }: { jobs: Job[] }) {
  const [repo, setRepo] = useState("");
  const [result, setResult] = useState("");

  const repos = useMemo(
    () => Array.from(new Set(jobs.map((j) => j.repo).filter(Boolean))).sort(),
    [jobs],
  );

  const visible = jobs.filter(
    (j) =>
      (!repo || j.repo === repo) &&
      (!result ||
        (result === "running" ? !j.ended_ts : j.ended_ts && j.conclusion === result)),
  );

  return (
    <section>
      <h2>
        Jobs <span className="muted">— what runners actually ran</span>
      </h2>
      <div className="row" style={{ marginBottom: ".5rem" }}>
        <select value={repo} onChange={(e) => setRepo(e.target.value)}>
          <option value="">all repos</option>
          {repos.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <select value={result} onChange={(e) => setResult(e.target.value)}>
          <option value="">any result</option>
          <option value="running">running</option>
          <option value="success">success</option>
          <option value="failure">failure</option>
          <option value="skipped">skipped</option>
        </select>
        <span className="muted">{visible.length} of {jobs.length}</span>
      </div>
      <div className="wrap">
        <table>
          <thead>
            <tr><th>when</th><th>repo</th><th>workflow / job</th><th>runner</th><th>result</th></tr>
          </thead>
          <tbody>
            {visible.length === 0 ? (
              <tr><td className="muted" colSpan={5}>no jobs match</td></tr>
            ) : visible.map((j) => {
              const running = !j.ended_ts;
              const href = safeUrl(j.url);
              const label = (
                <>
                  {j.workflow || "—"} / <strong>{j.job_name || "—"}</strong>
                </>
              );
              return (
                <tr key={j.job_id}>
                  <td className="muted">{ago(j.ts)}</td>
                  <td>{j.repo || "—"}</td>
                  <td>
                    {href ? (
                      <a href={href} target="_blank" rel="noopener noreferrer">{label}</a>
                    ) : label}
                  </td>
                  <td className="muted">{j.runner || "—"}</td>
                  <td>
                    {running ? <span className="warn">running</span>
                      : j.conclusion === "success" ? <span className="ok">success</span>
                      : j.conclusion === "failure" ? <span className="bad">failure</span>
                      : <span className="muted">{j.conclusion || "unknown"}</span>}
                    {j.ended_ts ? (
                      <span className="muted"> {Math.max(1, Math.round(j.ended_ts - j.ts))}s</span>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
