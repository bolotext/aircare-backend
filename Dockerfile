# Build context is aircare_backend/:  docker build -t manappuram_backend .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first (cache-friendly layer).
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src ./src

# No .env is baked into the image (secrets): config comes from real environment
# variables — docker-compose's `env_file: .env`, or `docker run --env-file .env`.
# pydantic-settings reads the environment first and ignores a missing .env file.

RUN useradd --system --no-create-home appuser
USER appuser

EXPOSE 8000
COPY --chown=appuser entrypoint.sh ./entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
