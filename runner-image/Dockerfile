FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV RUNNER_VERSION=2.323.0

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

# ── Pre-stage Node for actions/setup-node ────────────────────────────────────
# Stored in /opt/toolcache-staging; entrypoint copies to /opt/hostedtoolcache
# (which is a bind-mount from the host) on first boot.
RUN mkdir -p "/opt/toolcache-staging/node/${NODE_VERSION}/x64" \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
       | tar -xJ --strip-components=1 -C "/opt/toolcache-staging/node/${NODE_VERSION}/x64" \
    && touch "/opt/toolcache-staging/node/${NODE_VERSION}/x64.complete"

# ── Pre-stage Python for actions/setup-python ────────────────────────────────
# Build minimal Python from source into toolcache layout.
RUN apt-get update && apt-get install -y \
        build-essential zlib1g-dev libffi-dev libssl-dev \
        libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev \
    && curl -fsSL "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
       | tar -xJ -C /tmp \
    && cd "/tmp/Python-${PYTHON_VERSION}" \
    && ./configure --prefix="/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64" \
                   --enable-optimizations --with-lto \
    && make -j"$(nproc)" \
    && make install \
    && rm -rf "/tmp/Python-${PYTHON_VERSION}" \
    && touch "/opt/toolcache-staging/Python/${PYTHON_VERSION}/x64.complete" \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Create runner user
# GID 991 must match the host's docker group GID for socket access
RUN groupadd -f -g 991 docker \
    && useradd -m -s /bin/bash runner \
    && usermod -aG sudo runner \
    && usermod -aG docker runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Download GitHub Actions runner
WORKDIR /home/runner/actions-runner
RUN curl -o runner.tar.gz -L \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    && tar xzf runner.tar.gz \
    && rm runner.tar.gz \
    && ./bin/installdependencies.sh \
    && chown -R runner:runner /home/runner

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER runner
WORKDIR /home/runner/actions-runner

ENTRYPOINT ["/entrypoint.sh"]
