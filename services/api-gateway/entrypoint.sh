#!/bin/bash
set -e

# In Docker, PYTHONPATH is set by the image (services/api-gateway/src + libs/common/src).
# For running this script outside Docker (e.g. from the service dir directly),
# fall back to the local src/ layout.
export PYTHONPATH="${PYTHONPATH:-$(pwd)/src}"

# Production entrypoint: never --reload here (see main.py --help — it
# implies a single worker process, meant for local development only).
# WEB_CONCURRENCY sets the worker process count; default 2 is a reasonable
# starting point for this single-container-per-service docker-compose setup,
# which doesn't otherwise scale via replicas. Override per environment with
# `environment: WEB_CONCURRENCY: "N"` in docker-compose.yml.
exec python main.py --log-level "${LOG_LEVEL:-info}" --workers "${WEB_CONCURRENCY:-2}"
