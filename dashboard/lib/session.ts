// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

// Signed session cookies using node:crypto only.
//
// ponytail: no NextAuth / iron-session. This is a two-role login for one
// operator tool; a signed cookie is ~40 lines and adds no dependency to a
// service that sits in front of a Docker socket. Swap in an OIDC library the
// day real identity is needed, not before.

import { createHmac, timingSafeEqual } from "node:crypto";

export type Role = "admin" | "viewer";

export const SESSION_COOKIE = "rorch_session";
const MAX_AGE_SECONDS = 12 * 60 * 60;

export interface Session {
  role: Role;
  exp: number;
}

function secret(): string {
  const value = process.env.DASHBOARD_SESSION_SECRET;
  if (!value || value.length < 16) {
    // Failing closed is the point: a missing secret would otherwise mean
    // cookies signed with a guessable key, i.e. forgeable admin sessions.
    throw new Error("DASHBOARD_SESSION_SECRET must be set to at least 16 characters");
  }
  return value;
}

const b64 = (input: string) => Buffer.from(input, "utf8").toString("base64url");
const unb64 = (input: string) => Buffer.from(input, "base64url").toString("utf8");

function sign(payload: string): string {
  return createHmac("sha256", secret()).update(payload).digest("base64url");
}

export function createSession(role: Role): { value: string; maxAge: number } {
  const session: Session = { role, exp: Math.floor(Date.now() / 1000) + MAX_AGE_SECONDS };
  const payload = b64(JSON.stringify(session));
  return { value: `${payload}.${sign(payload)}`, maxAge: MAX_AGE_SECONDS };
}

export function readSession(cookie: string | undefined): Session | null {
  if (!cookie) return null;
  const [payload, signature] = cookie.split(".");
  if (!payload || !signature) return null;

  const expected = Buffer.from(sign(payload), "utf8");
  const supplied = Buffer.from(signature, "utf8");
  if (expected.length !== supplied.length || !timingSafeEqual(expected, supplied)) return null;

  try {
    const session = JSON.parse(unb64(payload)) as Session;
    if (session.exp < Math.floor(Date.now() / 1000)) return null;
    if (session.role !== "admin" && session.role !== "viewer") return null;
    return session;
  } catch {
    return null;
  }
}

/** Which password maps to which role. An unset password disables that role. */
export function roleForPassword(password: string): Role | null {
  const admin = process.env.DASHBOARD_ADMIN_PASSWORD ?? "";
  const viewer = process.env.DASHBOARD_VIEWER_PASSWORD ?? "";
  if (admin && constantTimeEquals(password, admin)) return "admin";
  if (viewer && constantTimeEquals(password, viewer)) return "viewer";
  return null;
}

function constantTimeEquals(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  if (left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}
