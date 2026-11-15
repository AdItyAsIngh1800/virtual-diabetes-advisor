"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.fixture(autouse=True)
def _hide_optional_settings_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep exported shell variables (e.g. ``ADMIN_API_KEY``) out of test settings.

    Real environment variables beat both ``.env`` and constructor defaults, so a
    developer shell that exports its ``.env`` would silently change tests that build
    ``Settings(_env_file=None, ...)``. Required settings are always passed explicitly
    by those tests; live tests read ``.env`` itself, which this does not touch.
    """
    for name, field in Settings.model_fields.items():
        if not field.is_required():
            monkeypatch.delenv(name.upper(), raising=False)
