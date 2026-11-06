# Manappuram Backend

Microservice backend for RBAC-gated call reporting: a dashboard with project-scoped access control, a paginated call list and call detail view (transcript, event timeline, tool results), page-1 summary metrics, CSV export, and a daily automailer (HTML summary, identical metrics to the dashboard, sent via the internal mail-relay HTTP API). See `docs/KNOWN_LIMITATIONS.md` for exactly what is and isn't real in this build, and `docs/SCHEMA_AND_INDEXES.md` for the full Mongo schema.

## Layout

One FastAPI app on **port 8000**, one `.env` (`./.env`, template in `.env.example`).

```
src/
  main.py            CLI: serve | seed | seed-demo | seed-calls | seed-patients | seed-risk-thresholds | schedule
  app.py             the single FastAPI app (all routes under /api/v1)
  users_service/     Organizations, users, roles/permissions, projects & assignments, JWT auth, /internal/*
  reports_service/   Call data, dashboard metrics, CSV export, automailer, patients, customer details, ...
  gateway/           thin proxy for /api/v1/mcp/* and /api/v1/voicebot/* (separate processes, if configured)
  common/            shared errors, responses, permissions, JWT helpers, .env location
tests/{users,reports}/
```

Reports resolves identity/project config from the Users module in-process (no network hop).

## Auto Scam (lead-score analytics)

`src/reports_service/auto_scam/` + frontend page **Auto Scam** (project workspace sidebar). Upload raw conversation JSON
(file name → `project_id`), CRUD the conversations, tick files and run the analytics from `auto_scam/scam/scam.py`
(called in-process; no files are written). Each result is stored in `auto_scam_results`, one row per `customerCRTId`
(previous vs updated stage code / lead score); a run **skips conversations that already have a successful result**
unless "Re-analyse already analysed" is ticked, and editing a conversation clears its result. Collections: `auto_scam`,
`auto_scam_results`, `auto_scam_datasets` (settings + prompt), `auto_scam_runs`. The Azure OpenAI credentials
(`AZURE_OPENAI_*`) go in this `.env`. The Conversations tab can preview and download the selected rows (or every filtered match) as CSV (`POST .../records/rows`, capped at 20,000 rows). Big files (hundreds of MB) upload in 8 MB chunks with a progress bar (`auto_scam/uploads.py`) and are parsed server-side in 1 MB blocks (~70 MB RAM), staged and swapped in only if the whole file parses. The Field mapper (Settings) chooses the extra columns shown in the tables: one column per entry, from the analytics JSON (e.g. REASON), the model output before the rules, what the rules changed (from → to, conditions), or a conversation field (e.g. a customer name). Each result keeps what the model said and what the rules changed (conditions + from -> to). Post-processing logic (STAGE_CODE / setupTime / … → INTEREST_CLASSIFICATION, DISPOSITION) is a rule list edited in the Settings tab. New permissions `auto_scam.view|manage|run` — re-run `make seed`.

## Roles & permissions

| Role | Scope | Can do |
|---|---|---|
| `ADMIN` | Global (no organization) | Manage users, organizations, and projects; view/export/schedule reports for **all** projects. |
| `SUPERVISOR` | Assigned projects only | View, export, and configure/run reports for their assigned project(s) only. |
| `VIEWER` | Assigned projects only | Read dashboard and call details for assigned project(s); **no** exports, **no** mailer access. |

Permission keys per role: `src/users_service/db/seed.py`.

## Running

```bash
cp .env.example .env            # then fill in the secrets (JWT_SECRET, INTERNAL_API_KEY, MONGO_URI, ...)
make install                     # uv sync  (dependencies live in pyproject.toml / uv.lock)

make seed                        # roles/permissions + bootstrap Admin
make seed-demo                   # demo org, 2 projects, a Supervisor and a Viewer (prints the ids)

make run                         # http://localhost:8000  (--reload, dev)
make run-prod                    # 2 worker processes, no reload (or set WEB_CONCURRENCY)
make schedule                    # the daily automailer loop — its own process
make test                        # all tests, against mongomock-motor (no Docker/Mongo needed)
```

Docker: `make docker-build && make docker-up` (one image, one container on :8000; config comes from `.env` at
run time — it is never copied into the image). Add the automailer with `docker compose --profile scheduler up -d`.

API docs: `http://localhost:8000/docs`. Seed commands that take ids:
`python src/main.py seed-calls --organization-id <id> --project-id <id> ...`,
`python src/main.py seed-patients --project-a-id <id> --project-b-id <id>`,
`python src/main.py seed-risk-thresholds`.

```bash
curl -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"<BOOTSTRAP_ADMIN_EMAIL>","password":"<BOOTSTRAP_ADMIN_PASSWORD>"}'
curl "http://localhost:8000/api/v1/dashboard?project_id=<id>" -H "Authorization: Bearer $TOKEN"
```

```bash
git push origin2 refs/remotes/origin/main:refs/heads/main
```

## Documentation

- [`docs/SCHEMA_AND_INDEXES.md`](docs/SCHEMA_AND_INDEXES.md) — every Mongo collection, its fields, and its indexes.
- [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) — what's simulated, what's out of scope, and why.
