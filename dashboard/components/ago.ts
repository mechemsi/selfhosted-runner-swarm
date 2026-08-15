// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

export function ago(ts: number | null | undefined): string {
  if (!ts) return "—";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}
