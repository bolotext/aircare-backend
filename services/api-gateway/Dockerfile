# Build context is the repo root (uv workspace): `docker build -f services/api-gateway/Dockerfile .`
FROM python:3.12-slim AS base
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG UV_VERSION=0.7.12
RUN curl -fsSL https://astral.sh/uv/$UV_VERSION/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Workspace metadata first (cache-friendly layer). uv needs every member's
# pyproject.toml present to validate the lockfile, even though we only
# install this one package below.
COPY pyproject.toml uv.lock /app/
COPY libs/common/pyproject.toml /app/libs/common/pyproject.toml
COPY services/api-gateway/pyproject.toml /app/services/api-gateway/pyproject.toml
COPY services/users/pyproject.toml /app/services/users/pyproject.toml
COPY services/reports/pyproject.toml /app/services/reports/pyproject.toml
COPY services/mcp_clients/pyproject.toml /app/services/mcp_clients/pyproject.toml
COPY services/scheduler/pyproject.toml /app/services/scheduler/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package api-gateway --compile-bytecode

COPY libs/common/src /app/libs/common/src
COPY services/api-gateway/src /app/services/api-gateway/src
COPY services/api-gateway/main.py /app/services/api-gateway/main.py
COPY services/api-gateway/entrypoint.sh /app/services/api-gateway/entrypoint.sh

RUN chmod +x /app/services/api-gateway/entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/services/api-gateway/src:/app/libs/common/src"

WORKDIR /app/services/api-gateway
EXPOSE 45461
ENTRYPOINT ["./entrypoint.sh"]
