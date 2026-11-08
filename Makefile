.PHONY: install test run runall run-prod seed seed-demo schedule docker-build docker-up docker-down docker-restart

# Everything is one app on :45461, configured by ./.env (see src/common/env.py).

install:
	uv sync

test:
	uv run pytest

run:
	@echo 'Backend on :45461 with --reload (dev only; needs a reachable MongoDB, see .env)'
	uv run python src/main.py serve --reload

# Backend + frontend (../aircare_frontend) in parallel; Ctrl-C stops both.
FRONTEND_DIR ?= ../aircare_frontend
runall:
	@trap 'kill 0' INT TERM; \
	$(MAKE) run & \
	$(MAKE) -C $(FRONTEND_DIR) run & \
	wait

run-prod:
	@echo 'Backend on :45461 with 2 worker processes, no reload'
	uv run python src/main.py serve --workers 2

pushorigin2:
	git push origin2 refs/remotes/origin/main:refs/heads/main

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
