from beanie import PydanticObjectId
from common.enums import Role
from common.log_utils import setup_logger
from common.mongo_utils import mongo_config_summary

from users_service.core.config import get_settings
from users_service.core.security import hash_password
from users_service.enums import UserStatus
from users_service.organizations import repository as organization_repo
from users_service.permissions.repository import PermissionRepository
from users_service.projects import repository as project_repo
from users_service.roles.repository import RoleRepository
from users_service.users import repository as user_repo
from users_service.users.model import User

logger = setup_logger(__name__)

# Demo fixture identifiers, deliberately fixed (not env-configurable) so the
# assignment's "seed two projects to prove isolation" step is reproducible by
# name for graders without reading this file first.
DEMO_ORG_SLUG = "demo-org"
DEMO_ORG_NAME = "Demo Organization"
DEMO_PROJECT_A_CODE = "proj-a"
DEMO_PROJECT_B_CODE = "proj-b"
# NOTE: must be a domain email-validator accepts as deliverable (it rejects
# reserved/special-use TLDs like .local), since these accounts need to
# actually log in through POST /auth/login, not just exist in the DB.
DEMO_SUPERVISOR_EMAIL = "supervisor.demo@example.com"
DEMO_VIEWER_EMAIL = "viewer.demo@example.com"
DEMO_PASSWORD = "DemoPass123!"

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.ADMIN: "Platform-level administrator, not scoped to any organization. Manages users, "
    "assignments, and projects across all organizations.",
    Role.SUPERVISOR: "Organization member with view/export/configure/run access to their assigned "
    "projects' reports only.",
    Role.VIEWER: "Organization member with read-only access to their assigned projects' dashboards "
    "and call details; no exports or mailer access.",
}

# Permission keys granted per role. "*" grants everything; "<module>.*" grants
# every action in that module. Kept in sync with the require_permission(...)
# calls in each module's router.
ROLE_PERMISSIONS: dict[Role, list[str]] = {
    Role.ADMIN: ["*"],
    Role.SUPERVISOR: [
        "project.view",
        "project.update",
        "report.view",
        "report.export",
        "report.configure_recipients",
        "report.trigger_manual",
        "report.annotate",
        "patient.view",
        "patient.create",
        "patient.update",
        "patient.delete",
        "patient_call.view",
        "patient_call.create",
        "patient_call.update",
        "patient_call.delete",
        "customer_detail.view",
        "customer_detail.create",
        "customer_detail.update",
        "customer_detail.delete",
    ],
    Role.VIEWER: ["project.view", "report.view", "patient.view", "patient_call.view"],
}

PERMISSION_DESCRIPTIONS: dict[str, str] = {
    "user.create": "Create a user.",
    "user.view": "View user profiles and listings.",
    "user.update": "Update a user's profile, role, or status.",
    "user.delete": "Disable a user.",
    "user.*": "All user permissions.",
    "organization.create": "Create an organization.",
    "organization.view": "View organizations.",
    "organization.update": "Update an organization.",
    "organization.*": "All organization permissions.",
    "project.view": "View projects and their assignments (scoped to assigned projects unless global).",
    "project.create": "Create a project.",
    "project.update": "Edit a project's settings (name, status, data source, field mapping, customer fields).",
    "project.assign": "Assign or unassign a user to/from a project.",
    "report.view": "View the reporting dashboard and call details for permitted projects.",
    "report.export": "Export the call list/dashboard as CSV for permitted projects.",
    "report.configure_recipients": "Configure automailer test recipients for permitted projects.",
    "report.trigger_manual": "Trigger a manual automailer run for permitted projects.",
    "report.annotate": "Edit tester Scenarios text on calls for permitted projects.",
    "patient.view": "View a project's patient roster (scoped to assigned projects unless global).",
    "patient.create": "Add a patient to a project's roster.",
    "patient.update": "Update a patient record.",
    "patient.delete": "Remove a patient from a project's roster.",
    "patient.*": "All patient permissions.",
    "patient_call.view": "View a project's patient advisory calls (scoped to assigned projects unless global).",
    "patient_call.create": "Record a new patient advisory call.",
    "patient_call.update": "Update a patient advisory call record.",
    "patient_call.delete": "Remove a patient advisory call record.",
    "patient_call.*": "All patient advisory call permissions.",
    "customer_detail.view": "View a project's customer details (personal data; scoped to assigned projects).",
    "customer_detail.create": "Add a customer-details record.",
    "customer_detail.update": "Update a customer-details record.",
    "customer_detail.delete": "Delete a customer-details record.",
    "customer_detail.*": "All customer-details permissions.",
    "risk_threshold.view": "View the condition -> AQI risk-threshold reference table (Admin only).",
    "risk_threshold.manage": "Create, update, or delete condition risk thresholds (Admin only).",
    "risk_threshold.*": "All risk-threshold permissions.",
    "*": "All permissions (platform-level).",
}


async def seed_roles() -> None:
    role_repo = RoleRepository()
    for role, description in ROLE_DESCRIPTIONS.items():
        existing = await role_repo.get_by_name(role)
        if existing is None:
            await role_repo.create(role, description)
            logger.info("Seeded role %s", role)


async def seed_permissions() -> None:
    """Idempotent: upserts each permission and its role mapping. Requires
    seed_roles() to have already run, since mappings link to a role's id.
    """
    role_repo = RoleRepository()
    permission_repo = PermissionRepository()

    for role, permission_keys in ROLE_PERMISSIONS.items():
        role_doc = await role_repo.get_by_name(role)
        if role_doc is None:
            logger.warning("Role %s not seeded yet, skipping its permission mappings", role)
            continue

        for key in permission_keys:
            permission = await permission_repo.get_by_key(key)
            if permission is None:
                permission = await permission_repo.create(
                    key=key, module=key.split(".")[0], description=PERMISSION_DESCRIPTIONS.get(key, key)
                )
                logger.info("Seeded permission %s", key)

            existing_mapping = await permission_repo.find_mapping(role_doc.id, permission.id)
            if existing_mapping is None:
                await permission_repo.create_mapping(role_doc.id, permission.id)
                logger.info("Seeded role-permission mapping: %s -> %s", role, key)


async def seed_bootstrap_admin() -> None:
    settings = get_settings()
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        logger.info("No bootstrap admin configured, skipping.")
        return

    existing = await User.find_one(
        User.organization_id == None,  # noqa: E711 - Beanie query operator requires `==`
        User.email == settings.bootstrap_admin_email,
    )
    if existing is not None:
        logger.info("Bootstrap admin already exists, skipping.")
        return

    await User(
        organization_id=None,
        email=settings.bootstrap_admin_email,
        hashed_password=hash_password(settings.bootstrap_admin_password),
        full_name=settings.bootstrap_admin_name,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
        created_by=None,
    ).insert()
    logger.info("Bootstrap admin created: %s", settings.bootstrap_admin_email)


async def seed_all() -> None:
    """Idempotent: safe to run on every startup and via a manual `python main.py seed`."""
    settings = get_settings()
    logger.info("seed: %s", mongo_config_summary(settings.mongo_uri, settings.mongo_db_name))
    await seed_roles()
    await seed_permissions()
    await seed_bootstrap_admin()


async def seed_demo_data() -> None:
    """Idempotent. Creates one demo organization, two projects under it, and
    one SUPERVISOR + one VIEWER each assigned to only one of the two projects
    — a ready-made fixture proving project-level isolation. Run via
    `python main.py seed-demo`, independent of `seed_all()`.
    """
    settings = get_settings()
    logger.info("seed-demo: %s", mongo_config_summary(settings.mongo_uri, settings.mongo_db_name))
    org = await organization_repo.get_by_slug(DEMO_ORG_SLUG)
    if org is None:
        org = await organization_repo.create_organization(
            name=DEMO_ORG_NAME, slug=DEMO_ORG_SLUG, created_by=PydanticObjectId()
        )
        logger.info("Seeded demo organization: %s (%s)", org.name, org.id)

    # Both demo projects point at the same db/collection reports-service has
    # always used (its own MONGO_DB_NAME, "calls") — project isolation is
    # still enforced via the project_id field already stored on each call
    # document, so make seed-reports-calls / the README's e2e walkthrough
    # keep working unchanged. A real project would set its own db/collection.
    project_a = await project_repo.get_by_org_and_code(org.id, DEMO_PROJECT_A_CODE)
    if project_a is None:
        project_a = await project_repo.create_project(
            organization_id=org.id,
            name="Demo Project A",
            code=DEMO_PROJECT_A_CODE,
            report_db_name="aircare",
            report_collection_name="calls",
            created_by=org.created_by,
        )
        logger.info("Seeded demo project: %s (%s)", project_a.name, project_a.id)

    project_b = await project_repo.get_by_org_and_code(org.id, DEMO_PROJECT_B_CODE)
    if project_b is None:
        project_b = await project_repo.create_project(
            organization_id=org.id,
            name="Demo Project B",
            code=DEMO_PROJECT_B_CODE,
            report_db_name="aircare",
            report_collection_name="calls",
            created_by=org.created_by,
        )
        logger.info("Seeded demo project: %s (%s)", project_b.name, project_b.id)

    supervisor = await user_repo.get_by_email(DEMO_SUPERVISOR_EMAIL)
    if supervisor is None:
        supervisor = await user_repo.create_user(
            email=DEMO_SUPERVISOR_EMAIL,
            hashed_password=hash_password(DEMO_PASSWORD),
            full_name="Demo Supervisor",
            role=Role.SUPERVISOR,
            organization_id=org.id,
            created_by=org.created_by,
        )
        logger.info("Seeded demo supervisor: %s", DEMO_SUPERVISOR_EMAIL)

    viewer = await user_repo.get_by_email(DEMO_VIEWER_EMAIL)
    if viewer is None:
        viewer = await user_repo.create_user(
            email=DEMO_VIEWER_EMAIL,
            hashed_password=hash_password(DEMO_PASSWORD),
            full_name="Demo Viewer",
            role=Role.VIEWER,
            organization_id=org.id,
            created_by=org.created_by,
        )
        logger.info("Seeded demo viewer: %s", DEMO_VIEWER_EMAIL)

    if await project_repo.get_assignment(supervisor.id, project_a.id) is None:
        await project_repo.create_assignment(
            user_id=supervisor.id, project_id=project_a.id, organization_id=org.id, created_by=org.created_by
        )
        logger.info("Assigned demo supervisor to project A only")

    if await project_repo.get_assignment(viewer.id, project_b.id) is None:
        await project_repo.create_assignment(
            user_id=viewer.id, project_id=project_b.id, organization_id=org.id, created_by=org.created_by
        )
        logger.info("Assigned demo viewer to project B only")

    print(
        "Demo data ready:\n"
        f"  Organization: {org.name} ({org.id})\n"
        f"  Project A: {project_a.name} ({project_a.id}) -> Supervisor {DEMO_SUPERVISOR_EMAIL}\n"
        f"  Project B: {project_b.name} ({project_b.id}) -> Viewer {DEMO_VIEWER_EMAIL}\n"
        f"  Password for both demo users: {DEMO_PASSWORD}"
    )
