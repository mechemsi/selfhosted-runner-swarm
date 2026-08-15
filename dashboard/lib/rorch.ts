// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

// Server-side access to the orchestrator API.
//
// The rorch tokens live here and are never sent to the browser: that is the
// whole point of the gateway. The browser holds a session cookie, the server
// holds the credential.

import type { Role } from "./session";

export const READ_METHODS = new Set(["GET", "HEAD"]);

/** Endpoints a viewer may reach. Anything else needs the admin role. */
const VIEWER_PREFIXES = [
  "/api/state",
  "/api/events",
  "/api/audit",
  "/api/jobs",
  "/api/history",
  "/api/health",
  "/api/config",
  "/metrics",
];

export function rorchBaseUrl(): string {
  return process.env.RORCH_URL ?? "http://orchestrator:8080";
}

export function tokenForRole(role: Role): string {
  if (role === "admin") return process.env.RORCH_API_TOKEN ?? "";
  // Falling back to the control token for a viewer would silently hand out
  // write access, so an unset read-only token means the viewer gets nothing
  // and rorch answers 401.
  return process.env.RORCH_API_READONLY_TOKEN ?? "";
}

/**
 * Whether this role may make this request.
 *
 * rorch enforces the same rule itself; this is the second lock, so a gateway
 * bug cannot promote a viewer into an operator.
 */
export function isAllowed(role: Role, method: string, path: string): boolean {
  if (role === "admin") return true;
  if (!READ_METHODS.has(method.toUpperCase())) return false;
  const normalised = path.startsWith("/") ? path : `/${path}`;
  // Container logs can contain secrets echoed by a job, so viewers are kept out.
  if (normalised.includes("/logs")) return false;
  return VIEWER_PREFIXES.some((prefix) => normalised === prefix || normalised.startsWith(`${prefix}?`) || normalised.startsWith(`${prefix}/`));
}

export async function callRorch(
  role: Role,
  method: string,
  path: string,
  body?: string,
  extraHeaders: Record<string, string> = {},
): Promise<Response> {
  const token = tokenForRole(role);
  const headers: Record<string, string> = {
    Accept: "application/json",
    // Surfaces the real actor in rorch's audit log instead of the gateway's IP.
    "X-Actor": `dashboard:${role}`,
    ...extraHeaders,
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body) headers["Content-Type"] = "application/json";

  return fetch(`${rorchBaseUrl()}${path}`, {
    method,
    headers,
    body,
    cache: "no-store",
  });
}
