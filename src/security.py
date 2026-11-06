"""Password hashing and JWT issuing/validation.

Pyodide's ``hashlib`` ships without ``pbkdf2_hmac``/``scrypt``, and a pure Python
PBKDF2 loop costs ~13us per iteration (100k would exceed the Workers CPU budget),
so hashing uses the runtime's WebCrypto PBKDF2 implementation. A pure Python
fallback keeps the code usable outside Workers (e.g. local scripts/tests), at a
reduced iteration count.
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
_SCHEME = "pbkdf2_sha256"


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


def _pbkdf2_python(password: bytes, salt: bytes, iterations: int) -> bytes:
    block = hmac.new(password, salt + b"\x00\x00\x00\x01", hashlib.sha256).digest()
    digest = block
    for _ in range(iterations - 1):
        block = hmac.new(password, block, hashlib.sha256).digest()
        digest = bytes(a ^ b for a, b in zip(digest, block))
    return digest


async def _pbkdf2_webcrypto(password: bytes, salt: bytes, iterations: int) -> bytes:
    import js
    from pyodide.ffi import to_js

    # The Workers WebCrypto binding requires a real JS Array for keyUsages;
    # a converted Python list is not accepted ("parameter 5 is not of type 'Array'").
    key_usages = js.Array.new()
    key_usages.push("deriveBits")
    key = await js.crypto.subtle.importKey(
        "raw", to_js(password), "PBKDF2", False, key_usages
    )
    algorithm = js.Object.new()
    algorithm.name = "PBKDF2"
    algorithm.salt = to_js(salt)
    algorithm.iterations = iterations
    algorithm.hash = "SHA-256"
    bits = await js.crypto.subtle.deriveBits(algorithm, key, 256)
    return bytes(bits.to_py())


async def _derive(password: bytes, salt: bytes, iterations: int) -> tuple[int, bytes]:
    """Derive a key, preferring WebCrypto and falling back to pure Python."""
    try:
        return iterations, await _pbkdf2_webcrypto(password, salt, iterations)
    except Exception:  # noqa: BLE001 - no WebCrypto outside the Workers runtime
        iterations = FALLBACK_ITERATIONS
        return iterations, _pbkdf2_python(password, salt, iterations)


async def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations, digest = await _derive(
        password.encode("utf-8"), salt, WEBCRYPTO_ITERATIONS
    )
    return f"{_SCHEME}${iterations}${_b64encode(salt)}${_b64encode(digest)}"


async def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
        if scheme != _SCHEME:
            return False
        salt = _b64decode(salt_b64)
        expected = _b64decode(digest_b64)
        _, digest = await _derive(password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def create_access_token(user_id: int, role: str, settings) -> tuple[str, int]:
    issued_at = utc_now_seconds()
    expires_at = issued_at + settings.jwt_exp_minutes * 60
    payload = {"sub": str(user_id), "role": role, "iat": issued_at, "exp": expires_at}
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str, settings) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
