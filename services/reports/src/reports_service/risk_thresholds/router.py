"""Admin-only CRUD for the condition -> AQI risk-threshold reference table.
No role other than ADMIN is ever granted the `risk_threshold.*` permission
(see users_service/db/seed.py's ROLE_PERMISSIONS), so `require_permission`
alone already excludes Supervisor/Viewer; the explicit `has_all_projects`
check below is defense-in-depth with a clearer error message, matching the
same belt-and-suspenders pattern used for the global automailer recipient
list (see reports.router.list_recipients).
"""

from common.errors import ConflictError, ForbiddenError, NotFoundError
from common.openapi import FORBIDDEN_EXAMPLE, UNAUTHORIZED_EXAMPLE, not_found_example, success_and_error_responses
from common.responses import success_response
from common.schemas import SuccessResponse
from fastapi import APIRouter, Depends, Path

from reports_service.core.deps import require_permission
from reports_service.core.users_client import IdentityContext
from reports_service.risk_thresholds import repository as threshold_repo
from reports_service.risk_thresholds.model import RiskThreshold
from reports_service.risk_thresholds.schema import RiskThresholdCreate, RiskThresholdOut, RiskThresholdUpdate

router = APIRouter(prefix="/risk-thresholds", tags=["risk-thresholds"])

_LIST_EXAMPLE = [RiskThresholdOut.model_config["json_schema_extra"]["example"]]
_DETAIL_EXAMPLE = RiskThresholdOut.model_config["json_schema_extra"]["example"]
_ADMIN_ONLY_EXAMPLE = FORBIDDEN_EXAMPLE


def _require_admin(identity: IdentityContext) -> None:
    if not identity.has_all_projects:
        raise ForbiddenError("Only an Admin may manage risk thresholds")


def _to_out(threshold: RiskThreshold) -> RiskThresholdOut:
    return RiskThresholdOut(
        condition=threshold.condition,
        risk_threshold_aqi=threshold.risk_threshold_aqi,
        pollutant=threshold.pollutant,
        is_seeded=threshold.is_seeded,
        created_at=threshold.created_at,
        updated_at=threshold.updated_at,
    )


@router.get(
    "",
    response_model=SuccessResponse[list[RiskThresholdOut]],
    summary="List condition risk thresholds",
    description="""Returns every configured condition -> AQI risk threshold, sorted by condition.

**When to use:** rendering the risk-threshold configuration screen.

**Errors:** 403 if the caller isn't an Admin. Requires `risk_threshold.view`.""",
    responses=success_and_error_responses("Risk thresholds", _LIST_EXAMPLE, UNAUTHORIZED_EXAMPLE, _ADMIN_ONLY_EXAMPLE),
)
async def list_risk_thresholds(
    identity: IdentityContext = Depends(require_permission("risk_threshold.view")),
) -> dict:
    _require_admin(identity)
    thresholds = await threshold_repo.list_all()
    return success_response(data=[_to_out(t) for t in thresholds])


@router.get(
    "/{condition}",
    response_model=SuccessResponse[RiskThresholdOut],
    summary="Get one condition's risk threshold",
    description="""Returns the configured risk threshold for a single condition.

**Errors:** 403 if the caller isn't an Admin; 404 if no threshold is configured for `condition`. Requires
`risk_threshold.view`.""",
    responses=success_and_error_responses("Risk threshold found", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, _ADMIN_ONLY_EXAMPLE, not_found_example("Risk threshold")),
)
async def get_risk_threshold(
    condition: str = Path(..., description="The condition to look up, e.g. `asthma`.", examples=["asthma"]),
    identity: IdentityContext = Depends(require_permission("risk_threshold.view")),
) -> dict:
    _require_admin(identity)
    threshold = await threshold_repo.get_by_condition(condition)
    if threshold is None:
        raise NotFoundError("Risk threshold not found")
    return success_response(data=_to_out(threshold))


@router.post(
    "",
    response_model=SuccessResponse[RiskThresholdOut],
    summary="Add a condition risk threshold",
    description="""Configures the AQI risk threshold for a condition.

**Required inputs:** `condition`, `risk_threshold_aqi`, `pollutant`.

**Errors:** 403 if the caller isn't an Admin; 409 if `condition` is already configured. Requires
`risk_threshold.manage`.""",
    responses=success_and_error_responses("Risk threshold created", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, _ADMIN_ONLY_EXAMPLE),
)
async def create_risk_threshold(
    body: RiskThresholdCreate,
    identity: IdentityContext = Depends(require_permission("risk_threshold.manage")),
) -> dict:
    _require_admin(identity)
    existing = await threshold_repo.get_by_condition(body.condition)
    if existing is not None:
        raise ConflictError(f"A risk threshold for '{body.condition}' already exists")
    threshold = await threshold_repo.create(
        condition=body.condition, risk_threshold_aqi=body.risk_threshold_aqi, pollutant=body.pollutant
    )
    return success_response(data=_to_out(threshold))


@router.patch(
    "/{condition}",
    response_model=SuccessResponse[RiskThresholdOut],
    summary="Update a condition risk threshold",
    description="""Partially updates a condition's risk threshold — only the fields supplied in the body
are changed.

**Errors:** 403 if the caller isn't an Admin; 404 if no threshold is configured for `condition`. Requires
`risk_threshold.manage`.""",
    responses=success_and_error_responses("Risk threshold updated", _DETAIL_EXAMPLE, UNAUTHORIZED_EXAMPLE, _ADMIN_ONLY_EXAMPLE, not_found_example("Risk threshold")),
)
async def update_risk_threshold(
    body: RiskThresholdUpdate,
    condition: str = Path(..., description="The condition to update, e.g. `asthma`.", examples=["asthma"]),
    identity: IdentityContext = Depends(require_permission("risk_threshold.manage")),
) -> dict:
    _require_admin(identity)
    threshold = await threshold_repo.get_by_condition(condition)
    if threshold is None:
        raise NotFoundError("Risk threshold not found")
    threshold = await threshold_repo.update(threshold, **body.model_dump())
    return success_response(data=_to_out(threshold))


@router.delete(
    "/{condition}",
    response_model=SuccessResponse[dict],
    summary="Remove a condition risk threshold",
    description="""Permanently removes a condition's configured risk threshold.

**Errors:** 403 if the caller isn't an Admin; 404 if no threshold is configured for `condition`. Requires
`risk_threshold.manage`.""",
    responses=success_and_error_responses("Risk threshold removed", {"deleted": True}, UNAUTHORIZED_EXAMPLE, _ADMIN_ONLY_EXAMPLE, not_found_example("Risk threshold")),
)
async def delete_risk_threshold(
    condition: str = Path(..., description="The condition to remove, e.g. `asthma`.", examples=["asthma"]),
    identity: IdentityContext = Depends(require_permission("risk_threshold.manage")),
) -> dict:
    _require_admin(identity)
    threshold = await threshold_repo.get_by_condition(condition)
    if threshold is None:
        raise NotFoundError("Risk threshold not found")
    await threshold_repo.delete(threshold)
    return success_response(data={"deleted": True})
