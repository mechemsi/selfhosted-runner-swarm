// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, createSession, roleForPassword } from "@/lib/session";

export const dynamic = "force-dynamic";

// Same reasoning as rorch's own guard: a password form on a LAN is worth
// nothing if it can be guessed at machine speed.
const MAX_ATTEMPTS = 10;
const LOCKOUT_MS = 5 * 60 * 1000;
const attempts = new Map<string, { count: number; last: number }>();

function clientKey(request: NextRequest): string {
  // Behind a reverse proxy this is the proxy; set TRUST_PROXY=1 only when a
  // proxy you control sets X-Forwarded-For, otherwise it is client-spoofable.
  if (process.env.TRUST_PROXY === "1") {
    const forwarded = request.headers.get("x-forwarded-for");
    if (forwarded) return forwarded.split(",")[0].trim();
  }
  return "shared";
}

function lockedFor(key: string): number {
  const entry = attempts.get(key);
  if (!entry || entry.count < MAX_ATTEMPTS) return 0;
  const remaining = entry.last + LOCKOUT_MS - Date.now();
  if (remaining <= 0) {
    attempts.delete(key);
    return 0;
  }
  return Math.ceil(remaining / 1000);
}

export async function POST(request: NextRequest) {
  const key = clientKey(request);
  const locked = lockedFor(key);
  if (locked > 0) {
    return NextResponse.json(
      { error: `too many attempts, try again in ${locked}s` },
      { status: 429, headers: { "Retry-After": String(locked) } },
    );
  }

  let password = "";
  try {
    password = ((await request.json()) as { password?: string }).password ?? "";
  } catch {
    return NextResponse.json({ error: "invalid request" }, { status: 400 });
  }

  const role = roleForPassword(password);
  if (!role) {
    const entry = attempts.get(key) ?? { count: 0, last: 0 };
    attempts.set(key, { count: entry.count + 1, last: Date.now() });
    return NextResponse.json({ error: "wrong password" }, { status: 401 });
  }

  attempts.delete(key);
  const session = createSession(role);
  const response = NextResponse.json({ role });
  response.cookies.set(SESSION_COOKIE, session.value, {
    httpOnly: true,
    sameSite: "strict",
    secure: process.env.COOKIE_SECURE !== "0",
    path: "/",
    maxAge: session.maxAge,
  });
  return response;
}
