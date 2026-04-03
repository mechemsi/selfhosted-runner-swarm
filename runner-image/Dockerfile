FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV RUNNER_VERSION=2.323.0

# Install dependencies + Docker CLI only (no daemon — uses host socket)
RUN apt-get update && apt-get install -y \
    curl \
    jq \
    git \
    unzip \
    tar \
    ca-certificates \
    gnupg \
    lsb-release \
    sudo \
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