import { readFileSync } from "node:fs";

// The release workflow passes RORCH_BUILD_VERSION into published images; a host
// build from a tag checkout falls back to the version semantic-release wrote
// into package.json. Inlined at build time, so the client bundle can show it.
const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8"));
const version = (process.env.RORCH_BUILD_VERSION || pkg.version).replace(/^v(?=\d)/, "");

/** @type {import('next').NextConfig} */
const nextConfig = {
  env: { RORCH_DASHBOARD_VERSION: version },
  // Standalone output keeps the runtime image small: no node_modules copy.
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};
export default nextConfig;
