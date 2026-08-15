// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

"use client";

import type { RunnerEvent } from "./types";
import { ago } from "./ago";

export default function Events({ events }: { events: RunnerEvent[] }) {
  return (
    <section>
      <h2>Events</h2>
      <div className="wrap">
        <table>
          <thead>
            <tr><th>when</th><th>event</th><th>pool</th><th>container</th><th>reason</th></tr>
          </thead>
          <tbody>
            {events.length === 0 ? (
              <tr><td className="muted" colSpan={5}>nothing recorded yet</td></tr>
            ) : events.map((e, i) => (
              <tr key={`${e.ts}-${e.container}-${i}`}>
                <td className="muted">{ago(e.ts)}</td>
                <td className={e.event.includes("fail") ? "bad" : e.event.includes("kill") ? "warn" : ""}>
                  {e.event}
                </td>
                <td>{e.pool || "—"}</td>
                <td className="muted">{e.container || "—"}</td>
                <td className="muted">{e.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
