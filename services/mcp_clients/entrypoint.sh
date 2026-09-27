#!/bin/bash
set -e

# In Docker, PYTHONPATH is set by the image (this service dir + libs/common/src).
# For running this script outside Docker (e.g. from the service dir directly),
# fall back to the local flat layout (no src/ — see pyproject.toml: package = false).
export PYTHONPATH="${PYTHONPATH:-$(pwd)}"

# Production entrypoint: never --reload here. WEB_CONCURRENCY sets the worker
# process count; default 2 is a reasonable starting point for this
# single-container-per-service docker-compose setup, which doesn't otherwise
# scale via replicas. Override per environment with
# `environment: WEB_CONCURRENCY: "N"` in docker-compose.yml.
exec python main.py serve --log-level "${LOG_LEVEL:-info}" --workers "${WEB_CONCURRENCY:-2}"
