// Copyright (c) 2026 Mechemsi. All rights reserved.
// Licensed under the MIT License. See LICENSE file in the project root.

import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "rorch — runner orchestrator",
  description: "GitHub Actions self-hosted runner orchestrator",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
