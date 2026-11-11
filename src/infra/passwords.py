"""Password hashing with the Workers WebCrypto fast path.

Pyodide's ``hashlib`` ships without ``pbkdf2_hmac``/``scrypt``, and a pure
Python PBKDF2 loop costs ~13us per iteration (100k would exceed the Workers
CPU budget), so hashing uses the runtime's WebCrypto PBKDF2 implementation.
``core.security`` provides the pure-Python fallback used outside Workers at a
reduced iteration count.
"""

from __future__ import annotations

import hmac
import secrets

from core.security import (
    FALLBACK_ITERATIONS,
    WEBCRYPTO_ITERATIONS,
    format_password_hash,
    parse_password_hash,
    pbkdf2_python,
)


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
        return iterations, pbkdf2_python(password, salt, iterations)


async def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations, digest = await _derive(
        password.encode("utf-8"), salt, WEBCRYPTO_ITERATIONS
    )
    return format_password_hash(salt, digest, iterations)


async def verify_password(password: str, stored: str) -> bool:
    parsed = parse_password_hash(stored)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    _, digest = await _derive(password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


# No 0/O or 1/l/I: the password is read off a screen and typed by a human.
_TEMP_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TEMP_PASSWORD_LENGTH = 16


def generate_temporary_password(length: int = TEMP_PASSWORD_LENGTH) -> str:
    """Random password for an admin-initiated reset (~93 bits at 16 chars)."""
    return "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(length))
