"""Password policy shared by registration and administrator resets.

Length is the primary defence, not character-class rules: a 20-character
passphrase beats ``Password1!`` even though the latter satisfies every "must
contain upper/lower/digit/symbol" checklist. The blocklist contains only
passwords of at least 8 characters, so it never duplicates the length check.
"""

from __future__ import annotations

import re
from itertools import pairwise

from core.common_passwords import common_passwords

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128

# The site is public and personal; a password containing its name is not a
# secret an internet attacker cannot guess.
_SITE_TERMS = (
    "linx-zhang",
    "linxzhang",
    "linx-zhang.github.io",
    "linx-api-78i",
    "api-worker",
)

_TRAILING_NOISE = re.compile(r"[^a-z0-9]+$")
_TRAILING_DIGITS = re.compile(r"\d+$")

# Normalize the substitutions people actually use. Two variants cover the
# ambiguous ``1``: both ``i`` (``p1zza``) and ``l`` (``he11o``).
_LEET_I = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "2": "z",
        "3": "e",
        "4": "a",
        "5": "s",
        "6": "g",
        "7": "t",
        "8": "b",
        "9": "g",
        "@": "a",
        "$": "s",
        "!": "i",
        "|": "i",
        "+": "t",
    }
)
_LEET_L = str.maketrans(
    {
        "0": "o",
        "1": "l",
        "2": "z",
        "3": "e",
        "4": "a",
        "5": "s",
        "6": "g",
        "7": "t",
        "8": "b",
        "9": "g",
        "@": "a",
        "$": "s",
        "!": "i",
        "|": "i",
        "+": "t",
    }
)


def _candidates(password: str) -> set[str]:
    """All cheap normalizations worth checking against the blocklist."""
    lower = password.lower()
    stripped = lower
    for _ in range(2):
        stripped = _TRAILING_NOISE.sub("", stripped)
        stripped = _TRAILING_DIGITS.sub("", stripped)
    bases = {lower, stripped}
    out: set[str] = set()
    for base in bases:
        out.add(base)
        for table in (_LEET_I, _LEET_L):
            translated = base.translate(table)
            out.add(translated)
            out.add(_TRAILING_DIGITS.sub("", translated))
    return {value for value in out if value}


def _is_repeated_or_sequential(value: str) -> bool:
    """Reject ``aaaaaaaa`` and ``abcdefgh`` / ``87654321`` without scoring."""
    if len(set(value)) == 1:
        return True
    diffs = {ord(right) - ord(left) for left, right in pairwise(value)}
    return len(diffs) == 1 and next(iter(diffs)) in {-1, 1}


def password_issue(password: str, *, email: str | None = None) -> str | None:
    """Return a human-readable reason the password is unacceptable, or None.

    The caller turns this into the stable ``weak_password`` API code. Login
    deliberately does not call this: existing accounts may predate the policy.
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"must be at least {PASSWORD_MIN_LENGTH} characters"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"must be at most {PASSWORD_MAX_LENGTH} characters"
    if not password.strip():
        return "must not contain only whitespace"

    candidates = _candidates(password)
    if any(candidate in common_passwords() for candidate in candidates):
        return "is a commonly used password"
    if _is_repeated_or_sequential(password.lower()):
        return "is too predictable; use a less repetitive password"

    email_lower = (email or "").strip().lower()
    local_part = email_lower.split("@", 1)[0]
    for needle in (email_lower, local_part):
        if len(needle) >= 3 and any(needle in candidate for candidate in candidates):
            return "must not contain your email address or its local part"

    # Compare the raw lowercase form: a site name is unlikely to be leet-encoded
    # in a way that survives normalization, and a false negative here is cheap.
    lower = password.lower()
    if any(term in lower for term in _SITE_TERMS):
        return "must not contain this site's name"
    return None
