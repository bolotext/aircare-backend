.PHONY: install test run run-prod seed seed-demo schedule docker-build docker-up docker-down docker-restart

# Everything is one app on :8000, configured by ./.env (see src/common/env.py).

install:
	python -m pip install -r requirements-dev.txt

test:
	python -m pytest

run:
	@echo 'Backend on :8000 with --reload (dev only; needs a reachable MongoDB, see .env)'
	python src/main.py serve --reload

run-prod:
	@echo 'Backend on :8000 with 2 worker processes, no reload'
	python src/main.py serve --workers 2

seed:
	python src/main.py seed

seed-demo:
	python src/main.py seed-demo

schedule:
	@echo 'Daily automailer loop (separate process; needs MongoDB + the mail relay, see .env)'
	python src/main.py schedule

docker-build:
	docker compose build

docker-up:
	@echo 'Starting the backend on :8000 (add --profile scheduler for the automailer: docker compose --profile scheduler up -d)'
	docker compose up -d

docker-down:
	docker compose down

docker-restart: docker-down docker-build docker-up
