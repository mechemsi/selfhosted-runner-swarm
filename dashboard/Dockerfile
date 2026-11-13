# syntax=docker/dockerfile:1
FROM node:26-bookworm-slim@sha256:5df79989ed685e09f0a98019db4cebfb22fb9df1a53c0161664a011f7ff6b0f1 AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
# npm ci when a lockfile is present, npm install to create one otherwise.
# The cache mount keeps downloaded tarballs in BuildKit's cache instead of
# re-fetching and re-extracting them on every rebuild.
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then npm ci; else npm install; fi

FROM node:26-bookworm-slim@sha256:5df79989ed685e09f0a98019db4cebfb22fb9df1a53c0161664a011f7ff6b0f1 AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Set by the release workflow; empty means "use package.json" (next.config.mjs).
ARG RORCH_BUILD_VERSION=""
RUN RORCH_BUILD_VERSION="${RORCH_BUILD_VERSION}" npm run build

FROM node:26-bookworm-slim@sha256:5df79989ed685e09f0a98019db4cebfb22fb9df1a53c0161664a011f7ff6b0f1 AS runtime
WORKDIR /app
# The standalone server binds $HOSTNAME, which Docker sets to the container ID —
# so it never listened on 127.0.0.1 and the HEALTHCHECK below always failed.
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
# Standalone output ships only the traced dependencies, not all of
# node_modules — a smaller image and a smaller attack surface next to a host
# running the Docker socket.
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
# The server is `node server.js`: the runtime never runs a package manager.
# Remove any bundled package managers, and pull the Debian security updates
# the base tag has not picked up yet (libpcre2, ...).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
              /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
              /usr/local/bin/yarn /usr/local/bin/yarnpkg /opt/yarn-*
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD node -e "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]
