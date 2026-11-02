# Build context is aircare_backend/:  docker build -t manappuram_backend .
FROM python:3.12-slim

# uv resolves/installs from pyproject.toml + uv.lock (no requirements.txt).
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first (cache-friendly layer): exactly what uv.lock pins, no dev group.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src

# No .env is baked into the image (secrets): config comes from real environment
# variables — docker-compose's `env_file: .env`, or `docker run --env-file .env`.
# pydantic-settings reads the environment first and ignores a missing .env file.

RUN useradd --system --no-create-home appuser
USER appuser

EXPOSE 8000
COPY --chown=appuser entrypoint.sh ./entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
