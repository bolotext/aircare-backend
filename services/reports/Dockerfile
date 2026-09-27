# Build context is the repo root (uv workspace): `docker build -f services/reports/Dockerfile .`
FROM python:3.12-slim AS base
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ARG UV_VERSION=0.7.12
RUN curl -fsSL https://astral.sh/uv/$UV_VERSION/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

COPY pyproject.toml uv.lock /app/
COPY libs/common/pyproject.toml /app/libs/common/pyproject.toml
COPY services/reports/pyproject.toml /app/services/reports/pyproject.toml
COPY services/users/pyproject.toml /app/services/users/pyproject.toml
COPY services/api-gateway/pyproject.toml /app/services/api-gateway/pyproject.toml
COPY services/mcp_clients/pyproject.toml /app/services/mcp_clients/pyproject.toml
COPY services/scheduler/pyproject.toml /app/services/scheduler/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package reports-service --compile-bytecode

COPY libs/common/src /app/libs/common/src
COPY services/reports/src /app/services/reports/src
COPY services/reports/main.py /app/services/reports/main.py
COPY services/reports/entrypoint.sh /app/services/reports/entrypoint.sh

RUN chmod +x /app/services/reports/entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/services/reports/src:/app/libs/common/src"

WORKDIR /app/services/reports
EXPOSE 8002
ENTRYPOINT ["./entrypoint.sh"]
