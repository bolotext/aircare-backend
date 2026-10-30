from beanie import PydanticObjectId
from common.enums import Role

from users_service.core.security import create_access_token, hash_password
from users_service.enums import UserStatus
from users_service.organizations.model import Organization
from users_service.projects.model import Project, UserProjectAssignment
from users_service.users.model import User


async def make_org(name: str, slug: str) -> Organization:
    org = Organization(name=name, slug=slug, created_by=PydanticObjectId())
    await org.insert()
    return org


async def make_user(
    organization_id: PydanticObjectId | None,
    role: Role,
    email: str,
    password: str = "password123",
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        organization_id=organization_id,
        email=email,
        hashed_password=hash_password(password),
        full_name=email,
        role=role,
        status=status,
    )
    await user.insert()
    return user


async def make_project(
    organization_id: PydanticObjectId,
    name: str,
    code: str,
    report_db_name: str = "aircare",
    report_collection_name: str = "calls",
) -> Project:
    project = Project(
        organization_id=organization_id,
        name=name,
        code=code,
        report_db_name=report_db_name,
        report_collection_name=report_collection_name,
        created_by=PydanticObjectId(),
    )
    await project.insert()
    return project


async def make_assignment(
    user_id: PydanticObjectId, project_id: PydanticObjectId, organization_id: PydanticObjectId
) -> UserProjectAssignment:
    assignment = UserProjectAssignment(
        user_id=user_id, project_id=project_id, organization_id=organization_id, created_by=PydanticObjectId()
    )
    await assignment.insert()
    return assignment


def auth_header(user: User) -> dict:
    token = create_access_token(
        str(user.id), str(user.organization_id) if user.organization_id else None, user.role
    )
    return {"Authorization": f"Bearer {token}"}


def data(resp) -> dict:
    """Unwrap the {status, code, message, data} success envelope."""
    return resp.json()["data"]


def code(resp) -> int:
    """The envelope's real status code. The HTTP transport status is always
    200 — see common.errors.register_exception_handlers.
    """
    return resp.json()["code"]


def error_type(resp) -> str:
    """Unwrap the {status, code, message, errors: {type, ...}} error envelope."""
    return resp.json()["errors"]["type"]
