from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

DataT = TypeVar("DataT")


class SuccessResponse(BaseModel, Generic[DataT]):
    """Envelope every successful API response is returned in. Pair with
    `common.responses.success_response()` when building the return value, and
    set `response_model=SuccessResponse[YourSchema]` on the route so the
    OpenAPI docs describe the real (wrapped) shape.
    """

    status: str = Field(default="success", description="Always `\"success\"` on this envelope.", examples=["success"])
    code: int = Field(
        default=200,
        description="Logical result code for this response (mirrors the HTTP status this call conceptually "
        "represents — 200, 201, ... ). See the API description for why the transport-level HTTP status is "
        "always 200 regardless of this value.",
        examples=[200],
    )
    message: str = Field(default="Success", description="Short, human-readable summary of the result.", examples=["Success"])
    data: DataT | None = Field(default=None, description="The endpoint's actual payload. Shape is endpoint-specific.")


class ErrorResponse(BaseModel):
    """Envelope every *failed* API response is returned in — same transport
    HTTP status (200) as `SuccessResponse`, but `status="error"` and `code`
    set to the logical failure status (400/401/403/404/409/422/429/502/500).
    Produced by `common.errors.register_exception_handlers`; every endpoint
    in this service can return this shape in addition to its documented
    `SuccessResponse[...]` shape — see the API description for why callers
    must branch on the body's `status`/`code`, not the transport status.
    """

    status: str = Field(default="error", description="Always `\"error\"` on this envelope.", examples=["error"])
    code: int = Field(
        description="The logical HTTP status this failure represents (400, 401, 403, 404, 409, 422, 429, 502, or "
        "500). The transport-level HTTP status is always 200 — check this field instead.",
        examples=[404],
    )
    message: str = Field(description="Human-readable error summary, safe to show to an end user.", examples=["Resource not found"])
    errors: dict[str, Any] | None = Field(
        default=None,
        description="Machine-readable error detail. Always includes `type` (a stable string such as "
        '`"NOT_FOUND"` or `"VALIDATION_ERROR"`); a 422 validation failure additionally includes a `fields` '
        "list describing exactly which input(s) failed and why.",
        examples=[{"type": "NOT_FOUND"}],
    )
