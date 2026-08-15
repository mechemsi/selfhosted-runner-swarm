// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { SESSION_COOKIE, readSession } from "@/lib/session";
import Dashboard from "@/components/Dashboard";

export const dynamic = "force-dynamic";

export default async function Home() {
  const session = readSession((await cookies()).get(SESSION_COOKIE)?.value);
  if (!session) redirect("/login");
  // The role is rendered, but never trusted: the gateway re-checks it on every
  // request, so hiding a button is convenience, not authorisation.
  return <Dashboard role={session.role} />;
}
