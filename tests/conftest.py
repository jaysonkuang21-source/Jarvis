"""Shared pytest fixtures for Jarvis tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.models import Profile


@pytest.fixture
def profile() -> Profile:
    """Minimal valid Profile for unit tests."""
    return Profile()


@pytest.fixture
def tmp_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Point settings at a tempdir and clear the settings cache around the test.

    Isolates scheduler SQLite and profile I/O. Forces the placeholder retrieval
    engine by clearing ``JARVIS_DATABASE_URL``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    profiles_path = tmp_path / "profiles.json"

    monkeypatch.setenv("JARVIS_DATABASE_URL", "")
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JARVIS_PROFILES_PATH", str(profiles_path))
    # Smoke tests exercise the API without minting a token; opt into lab mode.
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED_API", "true")
    # Avoid multi-second TCP waits to a closed Fish Speech port in every test.
    monkeypatch.setenv("JARVIS_TTS_ENABLED", "false")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.data_dir == data_dir
    assert settings.database_url == ""
    assert settings.profiles_path == profiles_path

    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    yield settings
    get_settings.cache_clear()
    from app.security import reset_policy_engine

    reset_policy_engine()
