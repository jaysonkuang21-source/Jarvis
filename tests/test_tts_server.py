"""Fish Speech Docker autostart helpers (no live Docker required in CI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.tts.server import fish_data_root, model_dir, model_ready


@pytest.fixture
def tts_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Isolate Fish data under a temp dir."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JARVIS_TTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TTS_AUTOSTART", "true")
    get_settings.cache_clear()
    settings = get_settings()
    yield settings
    get_settings.cache_clear()


def test_model_ready_false_without_weights(tts_settings: Settings) -> None:
    """Missing codec.pth means weights are not ready."""
    assert model_ready() is False
    root = fish_data_root()
    assert root.is_dir()
    assert (root / "checkpoints").is_dir()


def test_ensure_skips_without_weights(
    tts_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autostart must not call docker when weights are missing."""
    from app.tts.server import ensure_fish_speech

    monkeypatch.setattr("app.tts.fish_reachable", lambda: False)
    called: list[str] = []

    def boom(_args: list[str], *, check: bool = False):  # noqa: ARG001
        """Fail if docker is invoked."""
        called.append("docker")
        raise AssertionError("docker should not run without weights")

    monkeypatch.setattr("app.tts.server._run_docker", boom)
    assert ensure_fish_speech() is False
    assert called == []


def test_ensure_starts_existing_container(
    tts_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stopped container should be started when weights exist."""
    from app.tts.server import ensure_fish_speech, model_dir

    codec = model_dir() / "codec.pth"
    codec.parent.mkdir(parents=True, exist_ok=True)
    codec.write_bytes(b"fake")

    monkeypatch.setattr("app.tts.fish_reachable", lambda: False)
    monkeypatch.setattr("app.tts.server._docker_bin", lambda: "docker")
    monkeypatch.setattr("app.tts.server._container_state", lambda: "exited")
    monkeypatch.setattr("app.tts.server._start_existing", lambda: True)
    # After start, pretend API came up.
    monkeypatch.setattr(
        "app.tts.server._wait_until_ready", lambda _timeout: True
    )
    assert ensure_fish_speech() is True
