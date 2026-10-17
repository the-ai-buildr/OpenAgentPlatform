# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is kept in the final image because the compose healthcheck uses it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

FROM base AS runtime
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY ./app /app/app
COPY ./openwebui_functions /app/openwebui_functions

# The workspace and the read-only Open WebUI uploads mount live here.
RUN mkdir -p /app/workspace_files/general \
             /app/workspace_files/sops \
             /app/workspace_files/contracts \
             /app/openwebui_data \
    && useradd --create-home --uid 10001 oap \
    && chown -R oap:oap /app
USER oap

EXPOSE 8000

# No --reload: it doubles memory, breaks FastAPI lifespan with MCP tools, and
# has no place in an image. `make dev` adds it via the dev compose override.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
