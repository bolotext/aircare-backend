# Schema & Indexes

MongoDB collections across the two stateful services. `common.enums.Role` = `ADMIN | SUPERVISOR | VIEWER`.

## Users service (`aircare` database)

### `organizations`
| Field | Type | Notes |
|---|---|---|
| `name` | str | |
| `slug` | str | unique |
| `status` | enum | `active` \| `archived` |
| `created_by` | ObjectId | |
| `created_at`, `updated_at` | datetime | |

Indexes: `slug` (unique).

### `users`
| Field | Type | Notes |
|---|---|---|
| `organization_id` | ObjectId \| null | null only for `ADMIN` (global, unscoped) |
| `email` | str | unique |
| `hashed_password` | str | bcrypt |
| `full_name` | str | |
| `role` | enum | `ADMIN` \| `SUPERVISOR` \| `VIEWER` |
| `status` | enum | `active` \| `disabled` |
| `must_change_password` | bool | |
| `created_by` | ObjectId \| null | |
| `created_at`, `updated_at`, `last_login_at` | datetime | |

Indexes: `email` (unique, global — login takes no org context), `organization_id`.

### `roles`
Reference table seeded once at startup (`db/seed.py`), not user-editable. Fields: `name` (enum, unique), `description`, `created_at`.

### `permissions`
Reference table of fine-grained permission keys (`user.*`, `project.view`, `report.export`, `"*"`, etc). Fields: `key` (unique), `module`, `description`, `created_at`.

### `role_permission_mapping`
Many-to-many join between `roles` and `permissions`. Fields: `role_id`, `permission_id`, `created_at`. Indexes: `(role_id, permission_id)` unique.

### `refresh_tokens`
| Field | Type | Notes |
|---|---|---|
| `user_id` | ObjectId | |
| `token_hash` | str | SHA-256 of the raw refresh token — the raw token is never stored |
| `expires_at` | datetime | |
| `revoked` | bool | |
| `created_at` | datetime | |

Indexes: `user_id`; `token_hash` (unique); `expires_at` (TTL index, `expireAfterSeconds=0` — Mongo auto-deletes expired tokens).

### `projects`
| Field | Type | Notes |
|---|---|---|
| `organization_id` | ObjectId | |
| `name` | str | |
| `code` | str | short slug, unique per org |
| `report_db_name` | str | Mongo database reports-service reads this project's report data from |
| `report_collection_name` | str | collection within `report_db_name`, same shape as `calls` below |
| `status` | enum | `active` \| `archived` |
| `created_by` | ObjectId | |
| `created_at`, `updated_at` | datetime | |

Indexes: `(organization_id, code)` unique; `organization_id`.

`report_db_name`/`report_collection_name` are provided at project creation and let a project point reports-service at report data that already lives somewhere specific — reports-service reuses its single Mongo connection and just targets a different database/collection per project, rather than requiring every project's calls to live in one shared collection. Resolved via `GET /internal/projects/{project_id}` (internal-API-key-gated, like `/internal/introspect`) and consumed by `reports_service.core.deps.resolve_project_collection`.

### `user_project_assignments`
Grants a `SUPERVISOR`/`VIEWER` user access to one project. `ADMIN` needs no rows here — it's globally unrestricted (see `/internal/introspect`'s `has_all_projects`).

| Field | Type | Notes |
|---|---|---|
| `user_id` | ObjectId | |
| `project_id` | ObjectId | |
| `organization_id` | ObjectId | denormalized from the project, defense-in-depth for services querying this collection directly |
| `created_by` | ObjectId | |
| `created_at` | datetime | |

Indexes: `(user_id, project_id)` unique; `project_id`; `user_id`.

## Reports service (`aircare` database, separate collections)

### `calls`
A strict, indexed subset of fields for filtering/pagination/aggregation, plus a `raw` passthrough for the deeply-nested real call-record shape (`conversation_log`, `call_infra`, `disposition_result`, …) used only for call-detail rendering (transcript, event timeline, tool results). See `docs/KNOWN_LIMITATIONS.md` for why the full nested shape isn't typed.

This is the *default* location — the one every demo project and `seed_calls.py` fixture uses. `GET /calls`, `GET /calls/{conversation_id}`, and `GET /dashboard` all require `project_id` and actually read from that project's own `report_db_name`/`report_collection_name` (see the `projects` collection above), which happens to be this same collection for the seeded demo projects. A project pointing elsewhere must hold documents in this exact shape — CSV export and the automailer are unchanged and still always read this fixed collection.

| Field | Type | Notes |
|---|---|---|
| `project_id`, `organization_id` | ObjectId | |
| `conversation_id` | str | unique — the idempotency key for upserts |
| `bot_id` | str | |
| `language` | str | e.g. `kn-IN` |
| `call_status` | str | e.g. `ANSWERED`, `NO_ANSWER` |
| `disposition` | str | e.g. `RESOLVED`, `ABANDONED` |
| `call_start_time` | datetime | required — every metric/filter keys off this |
| `call_connected_time`, `call_end_time` | datetime \| null | |
| `connected_seconds` | float \| null | precomputed at write time |
| `tool_failure_count` | int | |
| `raw` | dict | passthrough — conversation_log, call_infra, disposition_result |
| `created_at` | datetime | |

Indexes: `(project_id, call_start_time desc)` — primary listing/pagination index; `(project_id, disposition)`; `(project_id, language)`; `conversation_id` (unique); `organization_id`.

### `report_runs`
Automailer run history/audit log.

| Field | Type | Notes |
|---|---|---|
| `run_key` | str | unique — `scheduled:<scope>:<date>` or `manual:<scope>:<date>:<uuid>` |
| `scope` | str | a project_id, or the literal `"all"` |
| `window_start`, `window_end` | datetime | start-inclusive/end-exclusive |
| `recipients` | list[str] | |
| `status` | enum | `pending` \| `running` \| `success` \| `failed` \| `ambiguous` \| `skipped_duplicate` |
| `attempts` | int | |
| `errors` | list[str] | |
| `triggered_by` | enum | `scheduled` \| `manual` |
| `created_at`, `updated_at` | datetime | |

Indexes: `run_key` (unique — this is what makes routine scheduled runs idempotent); `(scope, window_start desc)`.

### `report_recipients`
Permitted-user-configured extra recipients for a project's report (or global, `project_id=null`, ADMIN-only).

| Field | Type | Notes |
|---|---|---|
| `project_id` | ObjectId \| null | null = global/all-projects recipient |
| `email` | str | |
| `added_by` | ObjectId | |
| `created_at` | datetime | |

Indexes: `project_id`.

### `scenario_runs`
Persisted result of a fixed scenario checklist that backs the dashboard's "scenario measure". **No writer exists in this codebase** — see `docs/KNOWN_LIMITATIONS.md`.

| Field | Type | Notes |
|---|---|---|
| `run_at` | datetime | |
| `checks` | list[{name, passed, details}] | |
| `passed_count`, `total_count` | int | |
| `scenario_measure_pct` | float | |

Indexes: `run_at desc`.
