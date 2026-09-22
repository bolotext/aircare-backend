# Known Limitations

Plain statement of what this codebase does and does not do, so nothing here is mistaken for more than it is.

## Call ingestion is a seed script, not a live receiver

There is no webhook/callback HTTP endpoint that a voice platform pushes call records to. Call data is loaded by `services/reports/src/reports_service/seed/seed_calls.py`, which generates synthetic, schema-accurate fixtures and writes them through the same `upsert_call_from_payload()` function a real ingestion endpoint would call. That function is already idempotent (keyed on the unique `conversation_id`) and guards against out-of-order replay — see its docstring and `services/reports/tests/test_dedup_upsert.py`, which exercises "callback duplication/order" and "duplicate writes" directly against that function rather than against a live HTTP endpoint that doesn't exist here.

## MCP servers are out of scope for this repository

The Redis session-store, two-source AQI reconciliation, and teleconsultation slot-booking MCP servers referenced in the broader assignment brief are maintained in a separate, external system and are explicitly **not** part of this codebase. Consequences:

- `DashboardMetrics.scenario_measure_pct` / `scenario_measure_available` will read `null` / "N/A" on the dashboard, in CSV exports, and in automailer emails here, because nothing in this codebase writes a `ScenarioRun` record. `scenarios/model.py` defines the storage shape; `scenarios/service.py::get_latest_scenario_measure()` reads it; there is no internal write endpoint yet. This is an integration point, not a bug — wiring the external system's scenario-check results into this dashboard means having it call `POST /internal/scenario-runs` (not yet built) to persist a `ScenarioRun`.
- No MCP failure/recovery tests, AQI-disagreement tests, or slot-race-condition tests exist in this repository's test suites — those belong to the external MCP system's own test suite.

## Voice samples are simulated

Any "voice sample" fixtures in this repository (`services/reports/tests/fixtures/voice_samples/`) are synthetic text transcripts shaped like the real `Call.raw.conversation_log` array, each explicitly tagged `"simulated": true`. They are not real audio, and no STT/TTS pipeline exists in this codebase. They exist to demonstrate 4 non-MCP conversational scenarios (topic/persona switching, interruption/barge-in, clarification requests, wrong-identity/impersonation rejection) as data shapes the dashboard/reporting layer can render — not as evidence of a working voice agent.

## Automailer scope discovery is config-driven

`mailer/scheduler.py` cannot ask the Users service "list every project" — no internal, non-user-scoped listing endpoint exists for that. Instead, which scopes get a daily email is controlled by the `REPORT_SCOPES` environment variable (comma-separated project ids and/or the literal `"all"`), not auto-discovered from the Projects collection. See `services/reports/.env.example`.

## "Answered" and "successful disposition" are this project's own explicit definitions

The assignment brief asks for these to be defined explicitly but does not dictate the exact definitions. This codebase's choices (documented verbatim in `services/reports/src/reports_service/metrics/service.py`'s module docstring):

- **Answered** = `call_status ∈ {"ANSWERED", "COMPLETED"}` AND `call_connected_time` is set.
- **Successful dispositions** = `{"RESOLVED", "APPOINTMENT_BOOKED", "INFO_PROVIDED", "TRANSFERRED_SUCCESSFULLY"}`; everything else (`ABANDONED`, `NO_ANSWER`, `VOICEMAIL`, `TECHNICAL_FAILURE`, `NEUTRAL`, …) is unsuccessful.

These are assumptions, not requirements handed down externally — change the `ANSWERED_STATUSES` / `SUCCESSFUL_DISPOSITIONS` constants in `metrics/service.py` if a different definition is needed; every consumer (dashboard, CSV, email) picks up the change automatically since they all call the same `compute_metrics()`.

## Tests that need real infrastructure vs. `mongomock-motor`

The full test suite (`make test`, `make test-reports`) runs entirely against `mongomock-motor`, an in-memory Mongo double — no Docker or real database required. This was end-to-end-verified once against a real MongoDB, Mailhog, and the full gateway→reports→users chain (login, dashboard, cross-project 403s, CSV export, manual report trigger, captured email) via `docker compose up`; that verification is not itself part of the automated `pytest` suite and must be re-run manually against `docker-compose.yml` to reconfirm after future changes.
