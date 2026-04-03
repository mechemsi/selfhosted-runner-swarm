FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

RUN pip install pyyaml --break-system-packages

WORKDIR /app
COPY orchestrator.py .

CMD ["python3", "-u", "orchestrator.py"]
