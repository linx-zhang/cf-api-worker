"""Which paths an anonymous visitor may report.

The visitor endpoint has no authentication, so anything it accepts will
eventually be written by a scanner. This is not a security boundary — it is a
noise filter: only the site's own public content pages are recorded, so
``/.env`` and ``/wp-login.php`` never reach the analytics snapshots.

Pure functions only, so the rules can be tested without FastAPI.
"""

from __future__ import annotations

# Roots that actually serve public content. ``/`` is the landing page.
VISITOR_PATH_ROOTS = ("/blog", "/wiki")


def is_public_path(path: str) -> bool:
    """True for paths a visitor is allowed to report."""
    # Query strings and fragments are not part of the page identity here; the
    # stored path keeps them, but the allow-list judges the bare path only.
    bare = path.split("?", 1)[0].split("#", 1)[0]
    if bare == "/":
        return True
    return any(
        bare == root or bare.startswith(root + "/") for root in VISITOR_PATH_ROOTS
    )
