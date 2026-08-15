// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import type { Role, State } from "./types";
import Pools from "./Pools";
import Containers from "./Containers";
import Jobs from "./Jobs";
import Events from "./Events";

const POLL_MS = 5000;

export default function Dashboard({ role }: { role: Role }) {
  const router = useRouter();
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState("");
  const [stamp, setStamp] = useState("");

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/rorch/api/state", { credentials: "same-origin" });
      if (res.status === 401) {
        router.replace("/login");
        return;
      }
      if (!res.ok) {
        setError(((await res.json()) as { error?: string }).error ?? res.statusText);
        return;
      }
      setState((await res.json()) as State);
      setError("");
      setStamp(new Date().toLocaleTimeString());
    } catch (err) {
      setError((err as Error).message);
    }
  }, [router]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  async function act(path: string, body: unknown = {}): Promise<Response> {
    const res = await fetch(`/api/rorch${path}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok && res.status !== 409) {
      alert(((await res.clone().json()) as { error?: string }).error ?? res.statusText);
    }
    void refresh();
    return res;
  }

  async function signOut() {
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
    router.replace("/login");
  }

  const globals = state?.globals;
  const cap = globals
    ? `${globals.total_containers}/${globals.max_total_runners || "∞"}`
    : "—";
  const budget = state?.rate_limit?.remaining ?? null;

  return (
    <>
      <header>
        <h1>RORCH</h1>
        <span className="muted">
          <span className="pill">runners {cap}</span>{" "}
          <span className="pill">lifetime {globals?.max_runner_lifetime ?? 0}m</span>{" "}
          <span className="pill">github {budget ?? "—"}</span>{" "}
          {globals?.paused ? <span className="pill bad">PAUSED</span> : null}{" "}
          <span className="pill">{role === "admin" ? "operator" : "read-only"}</span>
        </span>
        <span style={{ flex: 1 }} />
        <span className="muted">
          {error ? <span className="bad">{error}</span> : `updated ${stamp}`}
        </span>
        {role === "admin" && globals ? (
          <button onClick={() => void act("/api/pause", { paused: !globals.paused })}>
            {globals.paused ? "resume all" : "pause all"}
          </button>
        ) : null}
        <button onClick={() => void signOut()}>sign out</button>
      </header>

      <main>
        {state ? (
          <>
            <Pools pools={state.pools} role={role} act={act} refresh={refresh} />
            <Containers containers={state.containers} role={role} act={act} />
            <Jobs jobs={state.jobs} />
            <Events events={state.events} />
          </>
        ) : (
          <section className="muted">{error ? "could not load state" : "loading…"}</section>
        )}
      </main>
    </>
  );
}
