.PHONY: install test run run-prod seed seed-demo schedule docker-build docker-up docker-down docker-restart

# Everything is one app on :45461, configured by ./.env (see src/common/env.py).

install:
	uv sync

test:
	uv run pytest

run:
	@echo 'Backend on :45461 with --reload (dev only; needs a reachable MongoDB, see .env)'
	uv run python src/main.py serve --reload

run-prod:
	@echo 'Backend on :45461 with 2 worker processes, no reload'
	uv run python src/main.py serve --workers 2

seed:
	uv run python src/main.py seed

seed-demo:
	uv run python src/main.py seed-demo

schedule:
	@echo 'Daily automailer loop (separate process; needs MongoDB + the mail relay, see .env)'
	uv run python src/main.py schedule

docker-build:
	docker compose build

docker-up:
	@echo 'Starting the backend on :45461 (add --profile scheduler for the automailer: docker compose --profile scheduler up -d)'
	docker compose up -d

docker-down:
	docker compose down

docker-restart: docker-down docker-build docker-up
