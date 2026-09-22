"""Stateless permission-string matching, shared across services that need to
interpret an already-resolved `permissions` list (e.g. from the Users
service's /internal/introspect response) without re-implementing wildcard
matching themselves.

Resolving *which* permissions a role grants stays exclusively in the Users
service (it owns the roles/permissions/role_permission_mapping collections);
this module only answers "does this already-resolved set satisfy that key".
"""

WILDCARD = "*"


def has_permission(granted: set[str] | list[str], required: str) -> bool:
    granted_set = granted if isinstance(granted, set) else set(granted)
    if WILDCARD in granted_set:
        return True
    if required in granted_set:
        return True
    module = required.split(".")[0]
    return f"{module}.{WILDCARD}" in granted_set
