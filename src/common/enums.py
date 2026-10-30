from enum import StrEnum


class Role(StrEnum):
    """Platform-wide RBAC roles. Shared because any service that gates access
    by role (not just the Users service that issues tokens) needs the same
    values to interpret the `role` claim on an access token.

    ADMIN is global/unscoped (organization_id=None, permission "*"). SUPERVISOR
    and VIEWER are org-scoped and further restricted to their assigned projects.
    """

    ADMIN = "ADMIN"
    SUPERVISOR = "SUPERVISOR"
    VIEWER = "VIEWER"
