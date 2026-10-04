FROM ubuntu:22.04

# Keeps the orchestrator's `docker image prune -a` from deleting this image
# while no runner container references it.
LABEL rorch.keep=true

ENV DEBIAN_FRONTEND=noninteractive

# Toolchain versions to pre-stage for actions/setup-* (avoids download on every run)
ARG NODE_VERSION=22.16.0
ARG PYTHON_VERSION=3.11.12

# Install dependencies + Docker CLI only (no daemon — uses host socket)
RUN apt-get update && apt-get install -y \
    curl \
    jq \
    git \
    unzip \
    tar \
    xz-utils \
    ca-certificates \
    gnupg \
    lsb-release \
    sudo \
    libatomic1 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install Docker CLI only
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
       https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI (needed by several workflows)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
       https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Point the agent and setup-* actions at the shared toolcache. Without these the
# agent uses _work/_tool and every job downloads its toolchain again. The
# orchestrator also passes them at spawn, so older images get them too.
ENV RUNNER_TOOL_CACHE=/opt/hostedtoolcache \
    AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache

# ── Pre-stage Node for actions/setup-node ────────────────────────────────────
# Stored in /opt/toolcache-staging; entrypoint copies to /opt/hostedtoolcache
# (which is a bind-mount from the host) on first boot.
RUN mkdir -p "/opt/toolcache-staging/node/${NODE_VERSION}/x64" \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
       | tar -xJ --strip-components=1 -C "/opt/toolcache-staging/node/${NODE_VERSION}/x64" \
    && touch "/opt/toolcache-staging/node/${NODE_VERSION}/x64.complete"

# Make pre-staged Node.js the default system node
RUN ln -sf "/opt/toolcache-staging/node/${NODE_VERSION}/x64/bin/node" /usr/local/bin/node \
    && ln -sf "/opt/toolcache-staging/node/${NODE_VERSION}/x64/bin/npm" /usr/local/bin/npm \
    && ln -sf "/opt/toolcache-staging/node/${NODE_VERSION}/x64/bin/npx" /usr/local/bin/npx

# ── Pre-stage Python for actions/setup-python ────────────────────────────────
# Build minimal Python from source into toolcache layout.
RUN apt-get update && apt-get install -y \
        build-essential zlib1g-dev libffi-dev libssl-dev \
        libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev \
    && curl -fsSL "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
       | tar -xJ -C /tmp \
    && cd "/tmp/Python-${PYTHON_VERSION}" \
    && ./configure --prefix="/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64" \
                   --enable-optimizations --with-lto --with-ensurepip=install \
    && make -j"$(nproc)" \
    && make install \
    && ln -sf pip3 "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64/bin/pip" \
    && ln -sf python3 "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64/bin/python" \
    && rm -rf "/tmp/Python-${PYTHON_VERSION}" \
    && touch "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64.complete" \
    && rm -rf /var/lib/apt/lists/*
# build-essential (installed above to compile Python) is deliberately kept: CI
# jobs need a C toolchain — Go's -race/cgo, node-gyp, Python C-extensions.

# ── Make pre-staged Python 3.11 the default system python ───────────────────
# Ubuntu 22.04 ships Python 3.10 which lacks datetime.UTC (3.11+).
# Symlink the pre-staged 3.11 onto /usr/local/bin so it shadows /usr/bin/python3.
RUN ln -sf "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64/bin/python3" /usr/local/bin/python3 \
    && ln -sf "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64/bin/python" /usr/local/bin/python \
    && ln -sf "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64/bin/pip3" /usr/local/bin/pip3 \
    && ln -sf "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64/bin/pip" /usr/local/bin/pip

# ── Install common CI tools globally ────────────────────────────────────────
RUN /usr/local/bin/pip install --no-cache-dir ruff pyright \
    && ln -sf "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64/bin/ruff" /usr/local/bin/ruff \
    && ln -sf "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64/bin/pyright" /usr/local/bin/pyright

# Create runner user. uid/gid 1000: the orchestrator's tmpfs work dir is
# mounted with uid=1000,gid=1000 (docker_client.RUNNER_UID).
#
# No docker group here. The socket's GID differs per host, so the entrypoint
# reads it from /var/run/docker.sock at start (as root), adds `runner` to a group
# with that GID and drops to `runner`. One published image works on every host;
# rorch.docker_gid=runtime tells the orchestrator not to rebuild it per host.
RUN useradd -m -s /bin/bash runner \
    && usermod -aG sudo runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers
LABEL rorch.docker_gid=runtime

# Declared here, after the toolchain layers, on purpose: an ARG/ENV change
# invalidates the cache of every instruction that follows it. Declared at the
# top, each agent bump (or each pinned older agent) recompiled Python from
# source and wrote a full duplicate set of layers to the host disk. Here, all
# versions share every layer above and only the agent download differs.
#
# Runner agent version. An ARG so a pool can pin an older agent:
#   docker build --build-arg RUNNER_VERSION=2.328.0 -t gh-runner:2.328.0 .
# Must be >= 2.327 for Node24-runtime actions (shivammathur/setup-php@v2 and
# the current actions/* releases); older agents fail with
# "'using: node24' is not supported".
ARG RUNNER_VERSION=2.337.0
ENV RUNNER_VERSION=${RUNNER_VERSION}

# Stamped so the orchestrator (and `docker image inspect`) can tell which agent
# version a tag actually contains.
LABEL rorch.runner_version=${RUNNER_VERSION}

# Download GitHub Actions runner
WORKDIR /home/runner/actions-runner
RUN curl -o runner.tar.gz -L \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz \
    && rm runner.tar.gz \
    && ./bin/installdependencies.sh \
    && chown -R runner:runner /home/runner

COPY entrypoint.sh /entrypoint.sh
# 0755, not `chmod +x`. COPY preserves the host file's mode, and `+x` only adds
# the execute bit — a host file at 0600 becomes 0711, which bash cannot run as
# the non-root `runner` user because executing a script also requires *read*
# permission. That fails at spawn with "/entrypoint.sh: Permission denied" and
# the runner dies before registering. Set the mode absolutely so the image does
# not depend on the checkout's permissions.
RUN chmod 0755 /entrypoint.sh

# Starts as root on purpose: the entrypoint joins the socket's group, then
# re-executes itself as `runner` (setpriv, from util-linux in the base image).
# The agent itself never runs as root; config.sh refuses to.
WORKDIR /home/runner/actions-runner

ENTRYPOINT ["/entrypoint.sh"]
