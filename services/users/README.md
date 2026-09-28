# Users Service

Owns organizations (tenants), users, roles, and JWT-based auth for the
platform. Runs on `:45452`. See the [repo root README](../../README.md) for
how this fits into the overall microservices layout.

## Stack

- **API**: FastAPI
- **DB**: MongoDB via Motor + Beanie (ODM)
- **Auth**: JWT (access + refresh), bcrypt password hashing
- **Tenancy**: every resource is scoped by an indexed `organization_id`; every
  org-scoped query goes through a repository function that always filters by
  it — a client can never supply its own `organization_id` to widen a query
- **RBAC**: `ADMIN` (platform-level, org-less; manages users, projects, and
  reports across all organizations) → `SUPERVISOR` / `VIEWER` (org-scoped,
  assigned to specific projects; no user-management access), enforced via a
  `require_permission(...)` FastAPI dependency backed by a role→permission
  mapping, not scattered `if` checks. `Role` itself lives in `libs/common`
  since other services will need the same enum to interpret a token's `role`
  claim.

## Module layout

Domain-modular, not layered globally: each of `organizations/`, `users/`, and
`auth/` is self-contained.

```
src/users_service/
  organizations/   router.py  service.py  repository.py  schema.py  model.py
  users/           router.py  service.py  repository.py  schema.py  model.py
  auth/            router.py  service.py  repository.py  schema.py  model.py
  roles/           model.py                       # seed-only reference collection
  mongo_explorer/  router.py                      # browse Mongo dbs/collections for project setup
  core/            config.py  security.py  deps.py  rate_limit.py  time.py
  db/              mongodb.py  seed.py
  enums.py         OrganizationStatus, UserStatus  (service-local)
  app.py           FastAPI app factory
```

`auth/` owns the `RefreshToken` model (a session/auth artifact) even though
`users/service.py` calls into `auth/repository.py` to revoke sessions on
disable/password-change — that cross-module call is expected in a modular
(not fully isolated) service; what matters is there's no import cycle back
the other way.

## Setup

```bash
cp .env.example .env   # then fill in real values, see below
uv sync                 # from the repo root — this is a workspace member
```

### Environment variables (`.env`)

| Variable | Purpose |
|---|---|
| `MONGO_URI` | MongoDB connection string |
| `MONGO_DB_NAME` | Database name |
| `JWT_SECRET` | HMAC signing secret for access/refresh tokens. Must be identical wherever a token this service issues gets verified directly (not via the gateway) — never commit a real one |
| `JWT_ALGORITHM` | Defaults to `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | Token lifetimes |
| `INTERNAL_API_KEY` | Shared secret for `/api/v1/internal/introspect`, used by other internal services to resolve a token without re-implementing JWT verification |
| `LOGIN_RATE_LIMIT` | e.g. `5/minute`, applied to `POST /auth/login` |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` / `_NAME` | Used only by the seed step below, to idempotently create the first `ADMIN`. Unset them after your first run in a shared environment. |

> `ADMIN` is always org-less (`organization_id` is `null`) and there is no API
> path to attach one to an organization or to escalate a `SUPERVISOR`/`VIEWER`
> to `ADMIN` — the bootstrap seed script is the only way one gets created.

### Run locally

```bash
uv run python main.py seed    # idempotent: seeds the 3 fixed roles + bootstrap Admin
uv run python main.py serve   # starts the API on :45452
```

### Tests

```bash
uv run pytest   # against an in-memory Mongo mock (mongomock-motor) — no real DB needed
```

Covers: auth flow (login/refresh/logout, disabled accounts), RBAC per role,
and that only the global Admin can manage users across organizations.

### API docs

`http://localhost:45452/docs` (Swagger) and `http://localhost:45452/openapi.json`.

## Curl walkthrough

Admin creates an org, then a Supervisor and a Viewer user in it. Talking to
this service directly on `:45452`; swap the base URL for
`http://localhost:45451` to go through the API Gateway instead.

```bash
BASE=http://localhost:45452/api/v1

# 1. Log in as the bootstrap Admin (seeded via BOOTSTRAP_ADMIN_* env vars)
ADMIN_TOKEN=$(curl -s -X POST $BASE/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"<your bootstrap password>"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

# 2. Admin creates an organization
ORG_ID=$(curl -s -X POST $BASE/organizations \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "Acme Corp", "slug": "acme-corp"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['id'])")

# 3. Admin creates a Supervisor and a Viewer in that org
curl -s -X POST $BASE/users -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"email\":\"supervisor@acme.example.com\",\"password\":\"SupervisorPass123!\",\"full_name\":\"Supervisor One\",\"role\":\"SUPERVISOR\",\"organization_id\":\"$ORG_ID\"}"

curl -s -X POST $BASE/users -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"email\":\"viewer@acme.example.com\",\"password\":\"ViewerPass123!\",\"full_name\":\"Viewer One\",\"role\":\"VIEWER\",\"organization_id\":\"$ORG_ID\"}"

# 4. Admin lists all users across orgs (Supervisor/Viewer get 403 here)
curl -s $BASE/users -H "Authorization: Bearer $ADMIN_TOKEN"
```

## RBAC summary

| | Organizations | Create/update/disable users | List/get users | Own profile |
|---|---|---|---|---|
| `ADMIN` | full access | any org | any org | yes |
| `SUPERVISOR` | none | no | no | yes |
| `VIEWER` | none | no | no | yes |

Role can never be escalated to `ADMIN` through the API (create or update) —
it is only ever seeded by the bootstrap script. `SUPERVISOR`/`VIEWER` gain
access to specific reporting projects via assignments (see the Projects
module), not via user-management permissions.

## Browsing Mongo for project setup

`GET /api/v1/mongo/databases` and `GET /api/v1/mongo/databases/{db_name}/collections`
(both `project.create`-gated, i.e. ADMIN-only) let a caller discover valid
values for a project's `report_db_name`/`report_collection_name` (see
`projects/schema.py::ProjectCreate`) instead of typing them blind. Both
always browse this service's own configured Mongo connection (`MONGO_URI` in
`.env`, via `db/mongodb.get_client()`) — no other server can be queried.
System databases/collections (`admin`, `local`, `config`, `system.*`) are
filtered out since they're never valid report sources.

## For other services

`POST /api/v1/internal/introspect` (guarded by `X-Internal-Api-Key`, not user
JWTs) lets another microservice resolve `user_id`, `organization_id`, and
`role` from an access token without re-implementing JWT verification itself.
