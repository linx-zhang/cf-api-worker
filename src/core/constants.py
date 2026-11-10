"""Response codes and human-readable detail text.

Both are part of the public API contract: keep the values stable. Detail
templates use ``str.format`` with a ``reason`` placeholder; callers must pass
the runtime message so the final text is unchanged.
"""

from __future__ import annotations

# --- Codes -----------------------------------------------------------------
# Short identifier used for successful responses.
CODE_OK = "ok"
# Business / validation errors.
CODE_VALIDATION_ERROR = "validation_error"
CODE_INVALID_EMAIL = "invalid_email"
CODE_INVALID_PASSWORD = "invalid_password"
CODE_INVALID_INVITE_CODE = "invalid_invite_code"
CODE_EMAIL_EXISTS = "email_exists"
CODE_INVALID_CREDENTIALS = "invalid_credentials"
CODE_UNAUTHORIZED = "unauthorized"
CODE_FORBIDDEN = "forbidden"
CODE_NOT_FOUND = "not_found"
CODE_METHOD_NOT_ALLOWED = "method_not_allowed"
CODE_HTTP_ERROR = "http_error"
CODE_INVALID_PARENT_COMMENT = "invalid_parent_comment"
CODE_INVITE_CODE_IN_USE = "invite_code_in_use"
CODE_INVALID_INVITE_ROLE = "invalid_invite_role"
CODE_INTERNAL_ERROR = "internal_error"

# --- Details ---------------------------------------------------------------
DETAIL_OK = "ok"
DETAIL_INTERNAL_ERROR = "internal server error"
DETAIL_NOT_FOUND = "resource not found"
DETAIL_METHOD_NOT_ALLOWED = "method not allowed"
DETAIL_UNAUTHORIZED = "invalid or missing access token"
DETAIL_FORBIDDEN = "permission denied"

# Validation templates: the suffix is the message reported by Pydantic.
DETAIL_INVALID_EMAIL = "email is invalid: {reason}"
DETAIL_INVALID_PASSWORD = "password must be 8-128 characters: {reason}"
DETAIL_INVITE_CODE_MALFORMED = "invitation code is malformed: {reason}"
DETAIL_VALIDATION_ERROR = "request is invalid: {reason}"
DETAIL_INVALID_VALUE = "invalid value"
DETAIL_INVALID_EMAIL_REASON = "invalid email address"

# Authentication / registration.
DETAIL_EMAIL_EXISTS = "email is already registered"
DETAIL_INVALID_CREDENTIALS = "email or password is incorrect"
DETAIL_INVITE_CODE_INVALID = "invitation code is invalid, expired or already used"
DETAIL_INVITE_CODE_CONFLICT = "invitation code was used concurrently, please retry"

# Posts and comments.
DETAIL_POST_NOT_FOUND = "post not found"
DETAIL_COMMENT_NOT_FOUND = "comment not found"
DETAIL_POST_CREATE_FAILED = "post was not created"
DETAIL_COMMENT_CREATE_FAILED = "comment was not created"
DETAIL_POST_DELETED = "post deleted"
DETAIL_COMMENT_DELETED = "comment deleted"
DETAIL_INVALID_PARENT_COMMENT = "parent comment does not belong to this post"

# Admin invite-code management.
DETAIL_INVITE_ROLE = "invite role must be internal or admin: {reason}"
DETAIL_INVITE_CODES_CREATED = "invite codes created"
DETAIL_INVITE_CODE_NOT_FOUND = "invite code not found"
DETAIL_INVITE_CODE_IN_USE = "invite code has already been used and cannot be deleted"
DETAIL_INVITE_CODE_DELETED = "invite code deleted"
