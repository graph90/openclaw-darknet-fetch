FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends tor \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY openclaw_fetch/ ./openclaw_fetch/
RUN pip install --no-cache-dir ".[pdf]"

EXPOSE 9050 9051
ENTRYPOINT ["openclaw-fetch", "mcp", "--network", "auto"]
