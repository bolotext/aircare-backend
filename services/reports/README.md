# Reports Service

RBAC-gated call reporting: paginated call list, call detail (transcript,
event timeline, tool results), dashboard metrics, CSV export, and the daily
automailer (scheduled HTML summary + CSV email, manual trigger, recipient
configuration). Runs on `:45454`. See the
[repo root README](../../README.md) for how this fits into the overall
microservices layout.

## Stack

- **API**: FastAPI
- **DB**: MongoDB via Motor + Beanie (ODM) — owns the fixed `calls` and
  `scenario_runs` collections (used as a fallback default only, e.g. for the
  automailer's combined "all projects" scope), but `GET /calls`,
  `GET /calls/{id}`, `GET /dashboard`, and `GET /export.csv` actually read
  from whichever Mongo database/collection the requested project is
  configured with (see "Per-project report source" below); identity/RBAC/
  project data lives in the Users service.
- **Auth**: never decodes JWTs itself. Every request calls the Users
  service's `POST /api/v1/internal/introspect` (see `core/users_client.py`)
  to resolve role, resolved permissions, and project scope. This means a
  role change or project reassignment in Users takes effect on this
  service's very next request too.
- **RBAC**: `require_permission("report.view" | "report.export")` FastAPI
  dependencies (see `core/deps.py`), backed by the permission list
  introspect returns — never a hardcoded role check; all four routes 403
  when a caller explicitly requests a project they aren't assigned to.

## Per-project report source

A project's `report_db_name`/`report_collection_name` (set at project
creation in the Users service — see `docs/SCHEMA_AND_INDEXES.md` at the repo
root) tell this service which Mongo database and collection to read that
project's report data from, using the *same* Mongo connection this service
already holds (`db/mongodb.get_client()`) — not a new connection per project.
`GET /calls`, `GET /calls/{conversation_id}`, `GET /dashboard`, and
`GET /export.csv` therefore all require `project_id`: since each project's
data can live in a different database, there is no single "all projects"
collection left to query when `project_id` is omitted. Every filter,
pagination, aggregation, and count these routes run — `list_paginated`,
`get_by_conversation_id`, `compute_metrics`, `list_all` — executes against
that resolved collection. `core/deps.py::resolve_project_source` (a FastAPI
dependency) is what resolves this (via `GET /internal/projects/{project_id}`
on the Users service); `core/deps.py::ensure_project_access` still
separately enforces that the caller may see that project at all.

### Schema adapters — a project's collection isn't always native `Call` shape

Not every project's configured collection is guaranteed to already store
documents in this service's own `Call` schema. `resolve_project_source` also
resolves a normalizer for the collection (see `calls/schema_adapters.py`,
keyed by `(report_db_name, report_collection_name)`) when it isn't — e.g.
`demo_collection.report_llm` is a call-center CDR export with entirely
different field names and no `project_id`/`conversation_id`/`call_start_time`
at all. When an adapter applies, `calls/repository.py::fetch_normalized_calls`
fetches the whole collection and normalizes every document into a Call (never
persisted); filtering, sorting, and pagination then happen in Python
(`matches_filter`) instead of a pushed-down Mongo query, and
`metrics/service.py::compute_metrics` mirrors this with its own
Python-side aggregation (`_compute_metrics_from_calls`) — both must be kept
in sync with the metric definitions below by hand, since there's no shared
Mongo aggregation pipeline to enforce it. A project with no adapter
registered for its configured collection is assumed to already be native
`Call` schema and takes the existing Mongo-query path unchanged.

An adapter also decides what PII from its source schema is safe to surface:
`normalize_report_llm` deliberately drops customer name/phone numbers, the
call recording path, and the raw conversation transcript rather than passing
them into `raw` — this service's API must never return them. Adding a new
adapter for another foreign schema should make the same call explicitly,
not assume the source fields are safe to pass through.

The daily automailer / manual `/run` trigger applies the same resolution for
a single-project scope. Its "all projects" combined scope is the one
exception — a report spanning every project has no single project
db/collection to resolve, so it still reads the fixed shared `calls`
collection (a documented simplification, not an oversight).

## Date filters

`date_from`/`date_to` on `GET /calls`, `GET /dashboard`, and
`GET /export.csv` are calendar dates (`YYYY-MM-DD`), not timestamps — both
inclusive. `calls/repository.py::day_range_bounds` converts them to the
`[start, end)` UTC instant window `build_mongo_query` filters
`call_start_time` on: `date_from` at 00:00:00 UTC that day, `date_to` at
00:00:00 UTC the *following* day (exclusive), so all of `date_to`'s day is
included.

## Known limitation: no live ingestion

There is no webhook/callback endpoint in this build — call data is loaded by
`seed/seed_calls.py` (synthetic, schema-accurate fixtures). The
`upsert_call_from_payload()` function it calls is what a real ingestion
endpoint would call too; it's already idempotent (keyed on
`conversation_id`) and out-of-order-safe (an optional `event_time` guard —
see its docstring), which is what the dedup/replay tests exercise directly.
See `docs/KNOWN_LIMITATIONS.md` at the repo root for the full list of
simulated/simplified pieces across the whole build.

## Metric definitions (page-1 dashboard)

Defined once in `metrics/service.py`'s module docstring and enforced by one
function, `compute_metrics()`, that the dashboard route, the CSV export, and
(Phase 3) the automailer all call — this is what makes "dashboard, CSV and
email use identical metric definitions" true by construction rather than by
convention:

- **Answered** = `call_status in {"ANSWERED", "COMPLETED"}` AND
  `call_connected_time` is set.
- **Avg connected seconds** = mean of `connected_seconds` over answered calls
  where it's not `None`. A call with a connect time but no end time is
  "incomplete" — counted in `total_calls`, excluded from this average, and
  surfaced separately as `incomplete_calls_count`.
- **Successful dispositions** = the explicit `SUCCESSFUL_DISPOSITIONS`
  constant (`RESOLVED`, `APPOINTMENT_BOOKED`, `INFO_PROVIDED`,
  `TRANSFERRED_SUCCESSFULLY`); everything else is unsuccessful.
- **Tool failures** = sum of `tool_failure_count` across filtered calls;
  `calls_with_tool_failure` is the secondary "how many calls had ≥1 failure"
  figure.
- **Unique call IDs** = distinct `conversation_id` count, computed
  explicitly rather than assumed equal to `total_calls`.
- Every ratio (`answer_rate_pct`, `avg_connected_seconds`,
  `success_rate_pct`) is `None` — never `0` — on a zero denominator.
  `metrics/formatting.py` renders `None` as the literal string `"N/A"` for
  CSV/email; the dashboard JSON leaves it as `null` for the frontend to
  render however it likes.
- **Scenario measure**: percentage of the fixed scenario checklist that
  passed in the most recent recorded run (see `scenarios/model.py`;
  populated by Phase 4's `scripts/run_scenario_checks.py`, not yet built).
  `scenario_measure_available=False` / `null` until the first run exists.
- **Language breakdown**: distinct-`language` counts over the filtered set,
  computed inside `compute_metrics()` alongside the disposition breakdown —
  the dashboard, CSV, and automailer email all show the same numbers.

## Automailer

`mailer/pipeline.py::run_report()` is the single send pipeline: it computes
metrics via `compute_metrics()` (same function the dashboard/CSV use),
renders the HTML via `mailer/render.py`, builds the CSV via
`metrics/csv_export.py`, and sends via `mailer/smtp_client.py`. Both the
daily scheduler (`mailer/scheduler.py`) and the manual `POST /run` route
(`reports/router.py`) call this exact function — they cannot diverge.

- **Window**: `mailer/window.py::previous_ist_calendar_day()` — the
  preceding Asia/Kolkata calendar day, keyed on call start time,
  start-inclusive/end-exclusive. Both the scheduler and the manual trigger
  use this one function.
- **Idempotency**: a scheduled run's `run_key = "scheduled:<scope>:<date>"`
  is unique-indexed on `ReportRun`; a second scheduled attempt for the same
  scope+day is rejected by that index and reported back as
  `status="skipped_duplicate"` without sending anything twice. A manual
  run's `run_key` always includes a fresh uuid, so `POST /run` can always be
  used to re-send a day's report on request — it is never blocked by the
  day's own scheduled run.
- **Retries**: bounded (`MAX_SMTP_ATTEMPTS`, default 3, short backoff) for
  clean transport failures (`mailer/smtp_client.py::TransientSendError`).
- **Known behavior — late call-data updates**: a report reflects call data
  exactly as of the moment it ran. If a call's disposition/status changes
  after that day's report has already been sent, there is no automatic
  re-send — a permitted user (Admin, or Supervisor for their own project)
  can trigger `POST /run` to regenerate and re-send the report with current
  data.
- **Known behavior — ambiguous SMTP acknowledgement**: if the SMTP `DATA`
  command times out before a clear ACK/NACK, the message may or may not
  have actually been delivered. This is deliberately **not** retried — a
  duplicate customer-facing email is worse than one report needing a manual
  check — the run is marked `status="ambiguous"` and surfaced via `GET
  /runs` for a human to reconcile against the mail server's own logs.
- **Scope discovery (documented simplification)**: the scheduler cannot ask
  the Users service "list every project" (no such internal endpoint exists
  for a non-user, service-to-service caller), so which scopes get a daily
  email is config-driven via `REPORT_SCOPES` (comma-separated project ids
  and/or the literal `"all"`), not auto-discovered.
- **Local demo**: point `SMTP_HOST`/`SMTP_PORT` at the `mailhog` container
  from the root `docker-compose.yml` and open `http://localhost:8025` to see
  delivered mail without any real mail provider credentials.

## Setup

```bash
cp .env.example .env
uv sync   # from the repo root — this is a workspace member
```

`USERS_SERVICE_URL` + `INTERNAL_API_KEY` must point at a running Users
service and match its own `INTERNAL_API_KEY`.

### Seeding demo data

```bash
# 1. In services/users: seed roles+bootstrap Admin, then the demo org/projects
cd services/users && uv run python main.py seed && uv run python main.py seed-demo
#    ^ prints the demo organization id and the two demo project ids

# 2. In services/reports: seed synthetic calls for those projects
cd services/reports && uv run python main.py seed-calls \
  --organization-id <org id> \
  --project-id <project A id> \
  --project-id <project B id>
#    ^ project B is deliberately left with zero calls (empty-data fixture)
```

### Run locally

```bash
uv run python main.py serve   # starts the API on :45454
```

### Tests

```bash
uv run pytest   # against an in-memory Mongo mock (mongomock-motor) — no real DB or Users service needed
```

RBAC/project-scoping tests inject a canned identity via
`app.dependency_overrides[get_current_identity]` rather than running a real
Users service — see `tests/factories.py::make_identity`.
