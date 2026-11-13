"""Permission codes and the role -> codes mapping.

Framework-free: routes, dependencies and ``/auth/me`` all read this single
mapping so the checks and the reported permissions cannot drift apart.

Codes use ``resource:action:scope``. Roles are the values stored in
``users.role``; ``PUBLIC`` is a pseudo role that describes unauthenticated read
access and is never returned to a client.
"""

from __future__ import annotations

PUBLIC = "PUBLIC"

ACCOUNT_READ_SELF = "account:read:self"
ACCOUNT_UPDATE_SELF = "account:update:self"

POST_READ_ANY = "post:read:any"
POST_CREATE_ANY = "post:create:any"
POST_UPDATE_OWN = "post:update:own"
POST_UPDATE_ANY = "post:update:any"
POST_DELETE_OWN = "post:delete:own"
POST_DELETE_ANY = "post:delete:any"

COMMENT_READ_ANY = "comment:read:any"
COMMENT_CREATE_ANY = "comment:create:any"
COMMENT_DELETE_OWN = "comment:delete:own"
COMMENT_DELETE_ANY = "comment:delete:any"

INVITE_CODE_CREATE_ANY = "invite_code:create:any"
INVITE_CODE_READ_ANY = "invite_code:read:any"
INVITE_CODE_DELETE_ANY = "invite_code:delete:any"

USER_READ_ANY = "user:read:any"
USER_UPDATE_ANY = "user:update:any"
USER_DELETE_ANY = "user:delete:any"
USER_RESET_PASSWORD_ANY = "user:reset_password:any"

ACTIVITY_WRITE_SELF = "activity:write:self"
ACTIVITY_READ_ANY = "activity:read:any"

# Shared clipboard snippets: creating one only needs a signed-in user (same
# shape as /memos), reading is public — the unguessable code is the capability.
CLIP_DELETE_OWN = "clip:delete:own"
CLIP_DELETE_ANY = "clip:delete:any"

# Shared by every signed-in role.
_MEMBER_PERMISSIONS = (
    ACCOUNT_READ_SELF,
    ACCOUNT_UPDATE_SELF,
    POST_CREATE_ANY,
    POST_UPDATE_OWN,
    POST_DELETE_OWN,
    COMMENT_CREATE_ANY,
    COMMENT_DELETE_OWN,
    ACTIVITY_WRITE_SELF,
    CLIP_DELETE_OWN,
)

_ADMIN_PERMISSIONS = (
    POST_UPDATE_ANY,
    POST_DELETE_ANY,
    COMMENT_DELETE_ANY,
    INVITE_CODE_CREATE_ANY,
    INVITE_CODE_READ_ANY,
    INVITE_CODE_DELETE_ANY,
    USER_READ_ANY,
    USER_UPDATE_ANY,
    USER_DELETE_ANY,
    USER_RESET_PASSWORD_ANY,
    ACTIVITY_READ_ANY,
    CLIP_DELETE_ANY,
)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "user": _MEMBER_PERMISSIONS,
    "internal": _MEMBER_PERMISSIONS,
    "admin": _MEMBER_PERMISSIONS + _ADMIN_PERMISSIONS,
}

# Only used to describe public endpoints; never merged into user permissions.
PUBLIC_PERMISSIONS = (POST_READ_ANY, COMMENT_READ_ANY)


def permissions_for_role(role: str) -> list[str]:
    """Return the codes granted to ``role`` (empty for unknown roles)."""
    return list(ROLE_PERMISSIONS.get(role, ()))


def has_permission(role: str, code: str) -> bool:
    """Check ``code`` against a role, including the ``PUBLIC`` pseudo role."""
    if role == PUBLIC:
        return code in PUBLIC_PERMISSIONS
    return code in ROLE_PERMISSIONS.get(role, ())
