# syntax=docker/dockerfile:1
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

RUN apt-get update && apt-get upgrade -y && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Wheels stay in BuildKit's cache mount, not in the image or a fresh download.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt --break-system-packages

# Read by rorch.version: a host build from a tag checkout reports the version
# semantic-release wrote into pyproject.toml for that tag.
COPY pyproject.toml .
COPY rorch/ rorch/

# Set by the release workflow for published images. Last, so a version change
# only rebuilds this metadata layer.
ARG RORCH_BUILD_VERSION=""
ENV RORCH_BUILD_VERSION=${RORCH_BUILD_VERSION}

CMD ["python3", "-u", "-m", "rorch"]
