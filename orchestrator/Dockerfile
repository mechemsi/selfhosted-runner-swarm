# syntax=docker/dockerfile:1
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Wheels stay in BuildKit's cache mount, not in the image or a fresh download.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt --break-system-packages

COPY rorch/ rorch/

CMD ["python3", "-u", "-m", "rorch"]
