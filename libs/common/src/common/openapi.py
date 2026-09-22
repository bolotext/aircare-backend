"""Reusable OpenAPI/Swagger documentation helpers.

Every endpoint in every service here always returns HTTP 200 at the
transport layer — success *and* failure (see
`common.errors.register_exception_handlers`). The real outcome lives in the
JSON body's `status`/`code` fields (see `common.schemas.SuccessResponse` /
`ErrorResponse`). Because of that, declaring literal `responses={403: ...}`,
`{404: ...}` etc. on a route would misdescribe what the server actually
sends back — Swagger would show status codes that never appear on the wire.

These helpers instead attach realistic *error-body* examples under the one
status code (200) every response actually uses, so Swagger UI still shows a
caller every outcome an endpoint can produce, without lying about transport
behavior. `API_ENVELOPE_NOTE` is the one canonical explanation of this
convention, meant to be included once in each service's top-level FastAPI
`description` (see e.g. reports_service.app / users_service.app) rather than
repeated on every endpoint.
"""

from typing import Any

API_ENVELOPE_NOTE = """
### Response envelope

Every endpoint responds with **HTTP 200** at the transport layer, whether it
succeeded or failed — check the JSON body instead of the HTTP status code:

- **Success** — `{"status": "success", "code": 200, "message": "...", "data": {...}}`
  (`code` may be 201 for a create). `data` holds the endpoint's real payload.
- **Failure** — `{"status": "error", "code": 404, "message": "...", "errors": {"type": "NOT_FOUND", ...}}`.
  `code` carries the logical status (400/401/403/404/409/422/429/502/500);
  `errors.type` is a stable, machine-readable error code your client can
  branch on. A 422 validation failure additionally includes `errors.fields`,
  a list of exactly which input(s) failed and why.

Each endpoint below documents the specific failure cases it can produce
(permission checks, not-found lookups, validation rules); all of them use
this same envelope.
"""


def error_example(summary: str, code: int, message: str, error_type: str, **extra_errors: Any) -> dict:
    """One named example of the `ErrorResponse` body shape, ready to drop
    into a route's `responses={200: {"content": {"application/json":
    {"examples": {...}}}}}` override alongside a success example.
    """

    return {
        "summary": summary,
        "value": {
            "status": "error",
            "code": code,
            "message": message,
            "errors": {"type": error_type, **extra_errors},
        },
    }


UNAUTHORIZED_EXAMPLE = error_example(
    "Missing or invalid bearer token", 401, "Authentication required", "UNAUTHORIZED"
)
FORBIDDEN_EXAMPLE = error_example(
    "Caller lacks the required permission", 403, "You do not have permission to perform this action", "FORBIDDEN"
)
VALIDATION_EXAMPLE = error_example(
    "A required field was missing or malformed",
    422,
    "Request validation failed",
    "VALIDATION_ERROR",
    fields=[{"loc": ["query", "project_id"], "msg": "Field required", "type": "missing"}],
)


def not_found_example(resource: str) -> dict:
    return error_example(f"{resource} not found", 404, f"{resource} not found", "NOT_FOUND")


def success_and_error_responses(success_summary: str, success_value: Any, *error_examples: dict) -> dict:
    """Builds a route's `responses=` override: one HTTP-200 entry whose
    `examples` cover both the success payload and every realistic error body
    that endpoint can produce (see module docstring for why this is all
    filed under 200 rather than separate 4xx/5xx entries).
    """

    examples = {"success": {"summary": success_summary, "value": success_value}}
    for example in error_examples:
        key = "error_" + example["summary"].lower().replace(" ", "_").replace("'", "")[:40]
        examples[key] = example
    return {
        200: {
            "description": "Always returned, whether the call succeeded or failed — see `status`/`code` in the body.",
            "content": {"application/json": {"examples": examples}},
        }
    }
