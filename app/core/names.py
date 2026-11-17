"""Patient names as practitioners see them: the first name only.

Every practitioner-facing response that names a patient goes through
:func:`first_name`, so the server, not the page, enforces the rule. It lives here
(not in a repository) so that every repository can import it without a cycle;
``app.db.audit_repository`` re-exports it.
"""

from __future__ import annotations


def first_name(full_name: str | None) -> str | None:
    """Return the first word of a name.

    Args:
        full_name: ``profiles.full_name``.

    Returns:
        The first name, or ``None`` when no name is stored.
    """
    parts = (full_name or "").split()
    return parts[0] if parts else None
