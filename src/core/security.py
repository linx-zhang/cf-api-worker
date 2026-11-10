"""Password hashing primitives and JWT issuing/validation.

Framework-free: no FastAPI and no Workers-specific imports. The runtime
WebCrypto fast path lives in ``infra.passwords``; everything here is stdlib
plus PyJWT, so it can be reused by local scripts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

import jwt

from core.utils import utc_now_seconds

WEBCRYPTO_ITERATIONS = 100_000
FALLBACK_ITERATIONS = 10_000
SCHEME = "pbkdf2_sha256"


def b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def pbkdf2_python(password: bytes, salt: bytes, iterations: int) -> bytes:
    block = hmac.new(password, salt + b"\x00\x00\x00\x01", hashlib.sha256).digest()
    digest = block
    for _ in range(iterations - 1):
        block = hmac.new(password, block, hashlib.sha256).digest()
        digest = bytes(a ^ b for a, b in zip(digest, block))
    return digest


def format_password_hash(salt: bytes, digest: bytes, iterations: int) -> str:
    return f"{SCHEME}${iterations}${b64encode(salt)}${b64encode(digest)}"


def parse_password_hash(stored: str) -> tuple[int, bytes, bytes] | None:
    """Split ``scheme$iterations$salt$digest``; return None when malformed."""
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
        if scheme != SCHEME:
            return None
        return int(iterations), b64decode(salt_b64), b64decode(digest_b64)
    except (ValueError, TypeError):
        return None


def hash_password(password: str, iterations: int = FALLBACK_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = pbkdf2_python(password.encode("utf-8"), salt, iterations)
    return format_password_hash(salt, digest, iterations)


def verify_password(password: str, stored: str) -> bool:
    parsed = parse_password_hash(stored)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    digest = pbkdf2_python(password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def create_access_token(user_id: int, settings) -> tuple[str, int]:
    """Issue a token carrying only the user id; roles are read from D1."""
    issued_at = utc_now_seconds()
    expires_at = issued_at + settings.jwt_exp_minutes * 60
    payload = {"sub": str(user_id), "iat": issued_at, "exp": expires_at}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str, settings) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
