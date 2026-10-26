from beanie import PydanticObjectId
from common.enums import Role
from common.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError

from users_service.core.deps import AuthContext
from users_service.enums import ProjectStatus
from users_service.organizations import repository as organization_repo
from users_service.projects import repository as project_repo
from users_service.projects.model import Project, UserProjectAssignment
from users_service.users import repository as user_repo


async def create_project(
    auth: AuthContext,
    organization_id: PydanticObjectId,
    name: str,
    code: str,
    report_db_name: str,
    report_collection_name: str,
    customer_details_collection_name: str | None = None,
) -> Project:
    if await organization_repo.get_by_id(organization_id) is None:
        raise NotFoundError("Organization not found")
    if await project_repo.get_by_org_and_code(organization_id, code) is not None:
        raise ConflictError("A project with this code already exists in this organization", {"field": "code"})
    return await project_repo.create_project(
        organization_id=organization_id,
        name=name,
        code=code,
        report_db_name=report_db_name,
        report_collection_name=report_collection_name,
        customer_details_collection_name=customer_details_collection_name,
        created_by=auth.user_id,
    )


async def list_projects(auth: AuthContext, organization_id: PydanticObjectId | None) -> list[Project]:
    if auth.role == Role.ADMIN:
        return await project_repo.list_all(organization_id=organization_id)

    project_ids = await project_repo.list_project_ids_for_user(auth.user_id)
    projects = await project_repo.list_by_ids(project_ids)
    if organization_id is not None:
        projects = [p for p in projects if p.organization_id == organization_id]
    return projects


async def _ensure_project_access(auth: AuthContext, project: Project) -> None:
    if auth.role == Role.ADMIN:
        return
    if await project_repo.get_assignment(auth.user_id, project.id) is None:
        raise ForbiddenError("You do not have access to this project")


async def get_project(auth: AuthContext, project_id: PydanticObjectId) -> Project:
    project = await project_repo.get_by_id(project_id)
    if project is None:
        raise NotFoundError("Project not found")
    await _ensure_project_access(auth, project)
    return project


async def update_project(
    auth: AuthContext,
    project_id: PydanticObjectId,
    name: str | None,
    status: ProjectStatus | None,
    field_mapping: dict[str, str] | None = None,
    report_db_name: str | None = None,
    report_collection_name: str | None = None,
    customer_details_collection_name: str | None = None,
    customer_details_fields: list[dict] | None = None,
) -> Project:
    project = await project_repo.get_by_id(project_id)
    if project is None:
        raise NotFoundError("Project not found")
    if field_mapping is not None:
        field_mapping = {k.strip(): v.strip() for k, v in field_mapping.items()}
        if any(not k or not v for k, v in field_mapping.items()):
            raise BadRequestError("Field mapping names must not be empty", {"field": "field_mapping"})
    if customer_details_fields is not None:
        keys = [f["key"].lower() for f in customer_details_fields]
        if len(set(keys)) != len(keys):
            raise BadRequestError("Customer field keys must be unique", {"field": "customer_details_fields"})
    return await project_repo.update_project(
        project,
        name=name,
        status=status,
        field_mapping=field_mapping,
        report_db_name=report_db_name,
        report_collection_name=report_collection_name,
        customer_details_collection_name=customer_details_collection_name,
        customer_details_fields=customer_details_fields,
    )


async def assign_user(
    auth: AuthContext, project_id: PydanticObjectId, user_id: PydanticObjectId
) -> UserProjectAssignment:
    project = await project_repo.get_by_id(project_id)
    if project is None:
        raise NotFoundError("Project not found")

    target_user = await user_repo.get_by_id_global(user_id)
    if target_user is None:
        raise NotFoundError("User not found")
    if target_user.role == Role.ADMIN:
        raise ConflictError("Admin users do not need project assignments", {"field": "user_id"})

    existing = await project_repo.get_assignment(user_id, project_id)
    if existing is not None:
        return existing
    return await project_repo.create_assignment(
        user_id=user_id, project_id=project_id, organization_id=project.organization_id, created_by=auth.user_id
    )


async def unassign_user(project_id: PydanticObjectId, user_id: PydanticObjectId) -> None:
    if await project_repo.get_by_id(project_id) is None:
        raise NotFoundError("Project not found")
    if not await project_repo.delete_assignment(user_id, project_id):
        raise NotFoundError("Assignment not found")


async def list_assignments(project_id: PydanticObjectId) -> list[UserProjectAssignment]:
    if await project_repo.get_by_id(project_id) is None:
        raise NotFoundError("Project not found")
    return await project_repo.list_assignments_for_project(project_id)
