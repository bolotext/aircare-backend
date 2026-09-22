#!/bin/bash
set -e

# In Docker, PYTHONPATH is set by the image (services/reports/src + libs/common/src).
# For running this script outside Docker (e.g. from the service dir directly),
# fall back to the local src/ layout.
export PYTHONPATH="${PYTHONPATH:-$(pwd)/src}"

# Production entrypoint: never --reload here (see main.py serve --help — it
# implies a single worker process, meant for local development only).
# WEB_CONCURRENCY sets the worker process count; default 2 is a reasonable
# starting point for this single-container-per-service docker-compose setup,
# which doesn't otherwise scale via replicas. Override per environment with
# `environment: WEB_CONCURRENCY: "N"` in docker-compose.yml. Doesn't apply to
# `schedule` (the automailer loop, run as its own separate container/command).
exec python main.py serve --log-level "${LOG_LEVEL:-info}" --workers "${WEB_CONCURRENCY:-2}"
