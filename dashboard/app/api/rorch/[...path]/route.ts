// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

// The gateway. Every browser call to the orchestrator goes through here so the
// rorch token stays server-side and the session's role decides what is allowed.

import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, readSession } from "@/lib/session";
import { callRorch, isAllowed } from "@/lib/rorch";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, path: string[]): Promise<NextResponse> {
  const session = readSession(request.cookies.get(SESSION_COOKIE)?.value);
  if (!session) {
    return NextResponse.json({ error: "not signed in" }, { status: 401 });
  }

  const search = request.nextUrl.search;
  const target = `/${path.join("/")}${search}`;

  if (!isAllowed(session.role, request.method, target)) {
    return NextResponse.json(
      { error: "your account is read-only" },
      { status: 403 },
    );
  }

  const body = ["GET", "HEAD"].includes(request.method) ? undefined : await request.text();
  const idempotencyKey = request.headers.get("Idempotency-Key");

  let upstream: Response;
  try {
    upstream = await callRorch(
      session.role,
      request.method,
      target,
      body || undefined,
      idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {},
    );
  } catch (error) {
    // A dead orchestrator must read as "orchestrator unreachable", not as a
    // dashboard crash — the operator is most likely here because it is down.
    return NextResponse.json(
      { error: `orchestrator unreachable: ${(error as Error).message}` },
      { status: 502 },
    );
  }

  const contentType = upstream.headers.get("content-type") ?? "application/json";
  const payload = await upstream.text();
  const response = new NextResponse(payload, {
    status: upstream.status,
    headers: { "content-type": contentType },
  });
  const replayed = upstream.headers.get("Idempotency-Replayed");
  if (replayed) response.headers.set("Idempotency-Replayed", replayed);
  return response;
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function PATCH(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
export async function DELETE(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await context.params).path);
}
