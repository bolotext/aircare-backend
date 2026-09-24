# AirCare Backend

Microservice backend for RBAC-gated call reporting: a dashboard with project-scoped access control, a paginated call list and call detail view (transcript, event timeline, tool results), page-1 summary metrics, CSV export, and a daily automailer (HTML summary, identical metrics to the dashboard, sent via the internal mail-relay HTTP API). See `docs/KNOWN_LIMITATIONS.md` for exactly what is and isn't real in this build, and `docs/SCHEMA_AND_INDEXES.md` for the full Mongo schema.

## Services

| Service | Port | Owns |
|---|---|---|
| `api-gateway` | 8000 | Reverse proxy only — no auth logic; each downstream service verifies its own JWT/RBAC. |
| `users` | 8001 | Organizations, users, roles/permissions, projects & project assignments, JWT auth, `/internal/introspect`. |
| `reports` | 8002 | Call data, dashboard metrics, CSV export, automailer (scheduler + manual trigger + recipients). |

MCP tool servers (Redis session store, AQI two-source reconciliation, teleconsultation slot booking) are maintained in a **separate, external system** and are not part of this repository — see `docs/KNOWN_LIMITATIONS.md`.

## Roles & permissions

| Role | Scope | Can do |
|---|---|---|
| `ADMIN` | Global (no organization) | Manage users, organizations, and projects; view/export/schedule reports for **all** projects. |
| `SUPERVISOR` | Assigned projects only | View, export, and configure/run reports for their assigned project(s) only. |
| `VIEWER` | Assigned projects only | Read dashboard and call details for assigned project(s); **no** exports, **no** mailer access. |

Full permission-key breakdown in `services/users/README.md`.

## Reproducible startup

```bash
cp services/users/.env.example services/users/.env
cp services/reports/.env.example services/reports/.env
cp services/api-gateway/.env.example services/api-gateway/.env

make docker-build
make docker-up          # mongo, users, api-gateway, reports

make seed                # roles/permissions + bootstrap Admin (users-service)
make seed-demo            # demo org, 2 projects, 1 Supervisor (project A only), 1 Viewer (project B only)
                          # ^ prints the org id and both project ids

make seed-reports-calls ORG_ID=<org id> PROJECT_IDS="<project A id> <project B id>"
                          # synthetic calls across 5 languages; project B is deliberately left
                          # with zero calls to exercise the empty-data path
```

Then, through the gateway (`localhost:8000`):

```bash
# Login as the bootstrap Admin
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"<BOOTSTRAP_ADMIN_PASSWORD>"}'

# Dashboard (Authorization: Bearer <access_token> from above)
curl http://localhost:8000/api/v1/dashboard?project_id=<project A id> -H "Authorization: Bearer $TOKEN"

# CSV export (ADMIN/SUPERVISOR only — VIEWER gets 403)
curl http://localhost:8000/api/v1/export.csv?project_id=<project A id> -H "Authorization: Bearer $TOKEN"

# Manually trigger the automailer for a project (delivered via ALERT_API_URL — see services/reports/.env.example)
curl -X POST http://localhost:8000/api/v1/run -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"scope":"<project A id>","extra_recipients":["qa-test@example.com"]}'
```

This exact sequence (real MongoDB via `docker compose`, not the mongomock test double) was run once end-to-end during development: seed → seed-demo → seed-reports-calls → login → dashboard → cross-project 403 → CSV export → manual report trigger → email delivered via the mail-relay API.

### Running tests

```bash
make test            # users-service, against mongomock-motor — no Docker needed
make test-reports     # reports-service, against mongomock-motor — no Docker needed
```

### Running services individually (no Docker)

Every service (`users`, `reports`, `api-gateway`) is a standard FastAPI app served by Uvicorn, with the
same **development vs. production** distinction FastAPI's own docs recommend: reload for local iteration,
no reload plus multiple worker processes for anything real.

**Development** — auto-restarts the process on every source change (this service's own `src/` and the
shared `libs/common/src/`); always a single worker process, so never use this for anything but local
iteration:

```bash
make run-users      # equivalent: cd services/users        && uv run python main.py serve --reload
make run-gateway    # equivalent: cd services/api-gateway   && uv run python main.py --reload
make run-reports    # equivalent: cd services/reports       && uv run python main.py serve --reload
make run-reports-scheduler   # the daily automailer loop — its own process, no reload/workers concept
```

**Production** — no `--reload`, multiple worker processes so the service actually uses more than one CPU
core (this repo doesn't otherwise scale via replicas — see "Docker (recommended)" below, which is what
`docker-compose.yml` already runs):

```bash
make run-users-prod      # equivalent: ... main.py serve --workers 2
make run-gateway-prod    # equivalent: ... main.py --workers 2
make run-reports-prod    # equivalent: ... main.py serve --workers 2

# or directly, e.g.:
cd services/users && uv run python main.py serve --workers 4 --log-level warning
```

`--workers` is ignored (with a warning) if combined with `--reload` — pick one. If `--workers` is omitted
entirely, Uvicorn falls back to the standard `WEB_CONCURRENCY` env var, else defaults to a single process.

#### Docker (recommended for anything beyond local dev)

`docker compose up` already runs every service in production mode: each `services/*/entrypoint.sh` starts
`main.py serve --workers "${WEB_CONCURRENCY:-2}"` with **no** `--reload`. `WEB_CONCURRENCY` is settable per
service in `docker-compose.yml` (commented out there, defaulting to 2) without rebuilding the image:

```bash
make docker-build
make docker-up      # mongo, users, api-gateway, reports — production mode, see above
```

## Documentation

- [`docs/SCHEMA_AND_INDEXES.md`](docs/SCHEMA_AND_INDEXES.md) — every Mongo collection, its fields, and its indexes.
- [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) — what's simulated, what's out of scope, and why.
- [`services/users/README.md`](services/users/README.md) — auth/RBAC/tenancy details.
- [`services/reports/README.md`](services/reports/README.md) — metric definitions, automailer behavior, and setup.
