# Build context is the repo root (uv workspace): `docker build -f services/mcp_clients/Dockerfile .`
FROM python:3.12-slim AS base
WORKDIR /app

# curl/ca-certificates for uv's installer; nodejs (ships npm) to install the
# mongodb-js/mongodb-mcp-server MCP server, which this service spawns as a
# subprocess (see mongo_mcp_client.py). uvx for the Redis MCP server
# subprocess comes bundled with the uv install below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates nodejs npm \
 && rm -rf /var/lib/apt/lists/*

ARG UV_VERSION=0.7.12
RUN curl -fsSL https://astral.sh/uv/$UV_VERSION/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Installed once here, pinned, rather than via `npx -y mongodb-mcp-server` at
# runtime: npx would re-resolve "latest" and re-extract into a shared
# ~/.npm/_npx cache on every container start, and this service's 2 uvicorn
# workers construct their MCP client lazily on first request — two workers
# racing to populate the same npx cache directory corrupts the install
# (ENOTEMPTY/TAR_ENTRY_ERROR, missing transitive deps like
# @modelcontextprotocol/node) and the spawned server crashes before the MCP
# handshake completes ("Connection closed"). A build-time global install
# sidesteps both the race and the per-request network dependency.
ARG MONGODB_MCP_SERVER_VERSION=3.0.4
RUN npm install -g mongodb-mcp-server@${MONGODB_MCP_SERVER_VERSION}

# Workspace metadata first (cache-friendly layer). uv needs every member's
# pyproject.toml present to validate the lockfile, even though we only
# install this one package below.
COPY pyproject.toml uv.lock /app/
COPY libs/common/pyproject.toml /app/libs/common/pyproject.toml
COPY services/mcp_clients/pyproject.toml /app/services/mcp_clients/pyproject.toml
COPY services/users/pyproject.toml /app/services/users/pyproject.toml
COPY services/api-gateway/pyproject.toml /app/services/api-gateway/pyproject.toml
COPY services/reports/pyproject.toml /app/services/reports/pyproject.toml
COPY services/scheduler/pyproject.toml /app/services/scheduler/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package mcp-clients-service --compile-bytecode

COPY libs/common/src /app/libs/common/src
# Flat layout (package = false in pyproject.toml) — no src/ subdir here.
COPY services/mcp_clients/*.py /app/services/mcp_clients/
COPY services/mcp_clients/booking /app/services/mcp_clients/booking
COPY services/mcp_clients/entrypoint.sh /app/services/mcp_clients/entrypoint.sh

RUN chmod +x /app/services/mcp_clients/entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/services/mcp_clients:/app/libs/common/src"

WORKDIR /app/services/mcp_clients
EXPOSE 8003
ENTRYPOINT ["./entrypoint.sh"]
