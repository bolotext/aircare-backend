.PHONY: install test test-reports seed seed-demo seed-reports-calls seed-reports-patients seed-reports-risk-thresholds seed-scheduler-demo run-users run-gateway run-reports run-reports-scheduler run-scheduler run-users-prod run-gateway-prod run-reports-prod docker-build docker-up docker-down docker-restart

install:
	@echo 'Installing all workspace packages with uv'
	uv sync

test:
	@echo 'Running the Users service test suite'
	uv run --package users-service pytest services/users/tests

test-reports:
	@echo 'Running the Reports service test suite'
	uv run --package reports-service pytest services/reports/tests

seed:
	@echo 'Seeding roles and the bootstrap Admin (Users service)'
	cd services/users && uv run python main.py seed

seed-demo:
	@echo 'Seeding a demo organization, two projects, and a Supervisor/Viewer to prove project isolation (Users service)'
	cd services/users && uv run python main.py seed-demo

seed-reports-calls:
	@echo 'Seeding synthetic call data for the demo projects (Reports service). Pass ids: make seed-reports-calls ORG_ID=... PROJECT_IDS="id1 id2"'
	cd services/reports && uv run python main.py seed-calls --organization-id $(ORG_ID) $(foreach id,$(PROJECT_IDS),--project-id $(id))

seed-reports-patients:
	@echo 'Seeding the demo patient roster, split across the two demo projects (Reports service). Pass ids: make seed-reports-patients PROJECT_A_ID=... PROJECT_B_ID=...'
	cd services/reports && uv run python main.py seed-patients --project-a-id $(PROJECT_A_ID) --project-b-id $(PROJECT_B_ID)

seed-reports-risk-thresholds:
	@echo 'Seeding the condition -> AQI risk-threshold reference table (Reports service)'
	cd services/reports && uv run python main.py seed-risk-thresholds

seed-scheduler-demo:
	@echo 'Seeding the two demo projects, 40-patient roster, and AQI risk thresholds (Scheduler service). Idempotent, safe to re-run.'
	cd services/scheduler && uv run python seed.py

# --- Development (auto-reload on source changes; single process — never use these in production) ---

run-users:
	@echo 'Running the Users service locally on :8001 with --reload (dev only; expects a reachable MongoDB, see services/users/.env)'
	cd services/users && uv run python main.py serve --reload

run-gateway:
	@echo 'Running the API Gateway locally on :8000 with --reload (dev only; expects the Users service reachable, see services/api-gateway/.env)'
	cd services/api-gateway && uv run python main.py --reload

run-reports:
	@echo 'Running the Reports service locally on :8002 with --reload (dev only; expects a reachable MongoDB and the Users service, see services/reports/.env)'
	cd services/reports && uv run python main.py serve --reload

run-all:
ifeq ($(shell uname),Darwin)
	@osascript -e 'tell application "Terminal" to do script "cd $(PWD)/services/users && uv run python main.py serve --reload"'
	@osascript -e 'tell application "Terminal" to do script "cd $(PWD)/services/api-gateway && uv run python main.py --reload"'
	@osascript -e 'tell application "Terminal" to do script "cd $(PWD)/services/reports && uv run python main.py serve --reload"'
else
	@gnome-terminal -- bash -c 'cd $(PWD)/services/users && uv run python main.py serve --reload; exec bash'
	@gnome-terminal -- bash -c 'cd $(PWD)/services/api-gateway && uv run python main.py --reload; exec bash'
	@gnome-terminal -- bash -c 'cd $(PWD)/services/reports && uv run python main.py serve --reload; exec bash'
endif

run-reports-scheduler:
	@echo 'Running the Reports service automailer scheduler loop (expects a reachable MongoDB and SMTP server, see services/reports/.env)'
	cd services/reports && uv run python main.py schedule

run-scheduler:
	@echo 'Running the AQI outbound advisory scheduler loop (expects a reachable MongoDB, see services/scheduler/.env)'
	cd services/scheduler && uv run python aqi_outbound_scheduler.py

# --- Production-style (no reload, multiple worker processes; for docker-compose this is what
# services/*/entrypoint.sh already runs — see WEB_CONCURRENCY there) ---

run-users-prod:
	@echo 'Running the Users service on :8001 with 2 worker processes, no reload (production-style)'
	cd services/users && uv run python main.py serve --workers 2

run-gateway-prod:
	@echo 'Running the API Gateway on :8000 with 2 worker processes, no reload (production-style)'
	cd services/api-gateway && uv run python main.py --workers 2

run-reports-prod:
	@echo 'Running the Reports service on :8002 with 2 worker processes, no reload (production-style)'
	cd services/reports && uv run python main.py serve --workers 2

docker-build:
	@echo 'Building all service images'
	docker compose build

docker-up:
	@echo 'Starting mongo + all services'
	docker compose up -d

docker-down:
	@echo 'Stopping all services'
	docker compose down

docker-restart: docker-down docker-build docker-up
