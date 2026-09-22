from datetime import datetime, timezone
from uuid import uuid4

from beanie import PydanticObjectId

from reports_service.calls.model import Call
from reports_service.core.users_client import IdentityContext

DEFAULT_PERMISSIONS = {
    "ADMIN": frozenset({"*"}),
    "SUPERVISOR": frozenset(
        {"project.view", "report.view", "report.export", "report.configure_recipients", "report.trigger_manual"}
    ),
    "VIEWER": frozenset({"project.view", "report.view"}),
}


async def make_call(project_id: PydanticObjectId, organization_id: PydanticObjectId, **overrides) -> Call:
    now = datetime.now(timezone.utc)
    defaults = dict(
        project_id=project_id,
        organization_id=organization_id,
        conversation_id=str(uuid4()),
        bot_id="test-bot",
        language="en-IN",
        call_status="ANSWERED",
        disposition="RESOLVED",
        call_start_time=now,
        call_connected_time=now,
        call_end_time=now,
        connected_seconds=60.0,
        tool_failure_count=0,
        raw={},
    )
    defaults.update(overrides)
    call = Call(**defaults)
    await call.insert()
    return call


def make_identity(
    role: str,
    project_ids: list[str] | None = None,
    has_all_projects: bool | None = None,
    permissions: frozenset[str] | None = None,
    user_id: str = "000000000000000000000001",
    organization_id: str = "000000000000000000000002",
) -> IdentityContext:
    resolved_has_all_projects = has_all_projects if has_all_projects is not None else (role == "ADMIN")
    return IdentityContext(
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        permissions=permissions if permissions is not None else DEFAULT_PERMISSIONS.get(role, frozenset()),
        project_ids=None if resolved_has_all_projects else frozenset(project_ids or []),
        has_all_projects=resolved_has_all_projects,
    )
