FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* && apt-get clean

WORKDIR /app

RUN pip install --no-cache-dir uv==0.8.15

COPY pyproject.toml ./
COPY uv.lock* ./
COPY src/ ./src/

RUN uv pip install --system --no-cache .

RUN adduser --disabled-password --gecos '' --shell /bin/bash appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "-m", "google_keep_mcp.server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8080"]
