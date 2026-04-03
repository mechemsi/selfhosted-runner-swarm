FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --break-system-packages

COPY rorch/ rorch/

CMD ["python3", "-u", "-m", "rorch"]
