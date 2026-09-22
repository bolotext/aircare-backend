from datetime import datetime, timezone

from beanie import PydanticObjectId

from users_service.enums import ProjectStatus
from users_service.projects.model import Project, UserProjectAssignment


async def create_project(
    organization_id: PydanticObjectId,
    name: str,
    code: str,
    report_db_name: str,
    report_collection_name: str,
    created_by: PydanticObjectId,
) -> Project:
    project = Project(
        organization_id=organization_id,
        name=name,
        code=code,
        report_db_name=report_db_name,
        report_collection_name=report_collection_name,
        created_by=created_by,
    )
    await project.insert()
    return project


async def get_by_id(project_id: PydanticObjectId) -> Project | None:
    return await Project.get(project_id)


async def get_by_org_and_code(organization_id: PydanticObjectId, code: str) -> Project | None:
    return await Project.find_one(Project.organization_id == organization_id, Project.code == code)


async def list_all(organization_id: PydanticObjectId | None = None, skip: int = 0, limit: int = 50) -> list[Project]:
    conditions = []
    if organization_id is not None:
        conditions.append(Project.organization_id == organization_id)
    return await Project.find(*conditions).skip(skip).limit(limit).to_list()


async def list_by_ids(project_ids: list[PydanticObjectId]) -> list[Project]:
    if not project_ids:
        return []
    return await Project.find({"_id": {"$in": project_ids}}).to_list()


async def update_project(
    project: Project, name: str | None = None, status: ProjectStatus | None = None
) -> Project:
    if name is not None:
        project.name = name
    if status is not None:
        project.status = status
    project.updated_at = datetime.now(timezone.utc)
    await project.save()
    return project


async def create_assignment(
    user_id: PydanticObjectId,
    project_id: PydanticObjectId,
    organization_id: PydanticObjectId,
    created_by: PydanticObjectId,
) -> UserProjectAssignment:
    assignment = UserProjectAssignment(
        user_id=user_id, project_id=project_id, organization_id=organization_id, created_by=created_by
    )
    await assignment.insert()
    return assignment


async def get_assignment(user_id: PydanticObjectId, project_id: PydanticObjectId) -> UserProjectAssignment | None:
    return await UserProjectAssignment.find_one(
        UserProjectAssignment.user_id == user_id, UserProjectAssignment.project_id == project_id
    )


async def delete_assignment(user_id: PydanticObjectId, project_id: PydanticObjectId) -> bool:
    assignment = await get_assignment(user_id, project_id)
    if assignment is None:
        return False
    await assignment.delete()
    return True


async def list_assignments_for_project(project_id: PydanticObjectId) -> list[UserProjectAssignment]:
    return await UserProjectAssignment.find(UserProjectAssignment.project_id == project_id).to_list()


async def list_project_ids_for_user(user_id: PydanticObjectId) -> list[PydanticObjectId]:
    assignments = await UserProjectAssignment.find(UserProjectAssignment.user_id == user_id).to_list()
    return [a.project_id for a in assignments]
