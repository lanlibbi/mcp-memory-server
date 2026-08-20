# Build stage
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Runtime stage
FROM python:3.12-slim

LABEL org.opencontainers.image.title="MCP Memory Server"
LABEL org.opencontainers.image.description="Persistent agent memory via Markdown + SQLite FTS5 for Blockbrain MCP"
LABEL org.opencontainers.image.source="https://github.com/lanlibbi/mcp-memory-server"
LABEL org.opencontainers.image.licenses="MIT"

# Install only what we need at runtime
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        "mcp[cli]>=1.0.0" \
        "starlette>=0.37.0" \
        "uvicorn>=0.30.0" \
        "python-multipart>=0.0.9"

WORKDIR /app
COPY src/ /app/src/

# Default data directory — mount a volume here
ENV MEMORY_DATA_DIR=/data/memories
ENV MEMORY_PORT=8080
ENV LOG_LEVEL=info

VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["python", "-m", "uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8080"]