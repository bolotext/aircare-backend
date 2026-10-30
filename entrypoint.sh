#!/bin/bash
set -e

# Production entrypoint: never --reload here. WEB_CONCURRENCY sets the worker
# process count (default 2); override per environment in docker-compose.yml.
exec python src/main.py serve --log-level "${LOG_LEVEL:-info}" --workers "${WEB_CONCURRENCY:-2}"
