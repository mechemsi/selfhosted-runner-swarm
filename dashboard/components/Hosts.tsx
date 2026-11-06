// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

"use client";

import type { Federation, HostMetrics, PeerSnapshot } from "./types";

interface Row {
  name: string;
  host: HostMetrics | null;
  pools: PeerSnapshot["pools"];
  status: string;
  statusClass: string;
}

function temp(host: HostMetrics | null, max: number) {
  if (!host || host.temp_c === null) return <span className="muted">unknown</span>;
  const c = host.temp_c;
  const cls = c >= max ? "bad" : c >= max - 10 ? "warn" : "ok";
  return <span className={cls}>{c.toFixed(0)} °C</span>;
}

function rows(own: HostMetrics | undefined, fed: Federation | null | undefined): Row[] {
  const self: Row = {
    name: fed?.self?.instance || "this host",
    host: fed?.self?.host ?? own ?? null,
    pools: fed?.self?.pools ?? {},
    status: fed?.hot ? "hot: no new runners" : "this host",
    statusClass: fed?.hot ? "bad" : "muted",
  };
  const peers = (fed?.peers ?? []).map((p): Row => ({
    name: p.snapshot?.instance || p.name,
    host: p.snapshot?.host ?? null,
    pools: p.snapshot?.pools ?? {},
    status: p.ok ? `peer, ${p.age_seconds ?? "?"}s ago` : `unreachable: ${p.error || "no data"}`,
    statusClass: p.ok ? "ok" : "bad",
  }));
  return [self, ...peers];
}

export default function Hosts({
  host,
  federation,
}: {
  host?: HostMetrics;
  federation?: Federation | null;
}) {
  const max = federation?.max_temp_c ?? 85;
  const mode = !federation
    ? "solo"
    : federation.enabled
      ? "federated scaling on"
      : "federated scaling off (view only)";
  return (
    <section>
      <h2>
        Hosts <span className="pill">{mode}</span>
      </h2>
      <div className="wrap">
        <table>
          <thead>
            <tr>
              <th>host</th>
              <th>free cores</th>
              <th>load</th>
              <th>memory free</th>
              <th>CPU temp</th>
              <th>pools (running / busy)</th>
              <th>status</th>
            </tr>
          </thead>
          <tbody>
            {rows(host, federation).map((r) => (
              <tr key={r.name}>
                <td>{r.name}</td>
                <td>{r.host ? `${r.host.free_cores} / ${r.host.cores}` : "—"}</td>
                <td>{r.host ? `${r.host.load1.toFixed(1)} on ${r.host.cpu_count}` : "—"}</td>
                <td>
                  {r.host
                    ? `${(r.host.mem_available_mb / 1024).toFixed(1)} / ${(r.host.mem_total_mb / 1024).toFixed(0)} GB`
                    : "—"}
                </td>
                <td>{temp(r.host, max)}</td>
                <td className="muted">
                  {Object.entries(r.pools)
                    .map(([name, c]) => `${name} ${c.running}/${c.busy}`)
                    .join(", ") || "—"}
                </td>
                <td className={r.statusClass}>{r.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
