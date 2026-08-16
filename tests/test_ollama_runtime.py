"""Tests for Ollama keep_alive helpers and warm-on-boot."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.config import get_settings
from app.models import Profile, Provider
from app.ollama_runtime import (
    keep_alive_as_seconds,
    normalize_keep_alive,
    warm_ollama_from_profile,
    warm_ollama_model,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("30m", "30m"),
        (-1, -1),
        (120, 120),
    ],
)
def test_normalize_keep_alive(value: str | int | None, expected: str | int | None) -> None:
    """Blank strings become None; ints and duration strings pass through."""
    assert normalize_keep_alive(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("30m", 1800),
        ("1h", 3600),
        ("45s", 45),
        ("-1", -1),
        (-1, -1),
        (90, 90),
        ("not-a-duration", None),
    ],
)
def test_keep_alive_as_seconds(value: str | int | None, expected: int | None) -> None:
    """Duration strings convert to seconds for OllamaEmbeddings."""
    assert keep_alive_as_seconds(value) == expected


def test_warm_ollama_model_posts_generate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warm must POST /api/generate with model + keep_alive and no prompt."""
    get_settings.cache_clear()
    captured: dict[str, Any] = {}

    class FakeResponse:
        """Stand-in for httpx.Response that always succeeds."""

        def raise_for_status(self) -> None:
            """No-op success path."""

    class FakeClient:
        """Record the generate request then exit the context manager."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Ignore timeout/args; state lives on ``captured``."""

        def __enter__(self) -> FakeClient:
            """Return self as the active client."""
            return self

        def __exit__(self, *args: Any) -> None:
            """Nothing to clean up."""

        def post(self, url: str, json: dict[str, Any] | None = None) -> FakeResponse:
            """Capture URL and payload for assertions."""
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    assert warm_ollama_model("qwen3.5:9b", "30m") is True
    assert captured["url"].endswith("/api/generate")
    assert captured["json"]["model"] == "qwen3.5:9b"
    assert captured["json"]["keep_alive"] == "30m"
    assert captured["json"]["stream"] is False
    get_settings.cache_clear()


def test_warm_ollama_model_best_effort_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network failures must return False instead of raising."""

    class BoomClient:
        """Raise on enter to simulate unreachable Ollama."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Unused constructor kwargs."""

        def __enter__(self) -> BoomClient:
            """Fail immediately like a refused connection."""
            raise httpx.ConnectError("refused")

        def __exit__(self, *args: Any) -> None:
            """Unused."""

    monkeypatch.setattr(httpx, "Client", BoomClient)
    assert warm_ollama_model("qwen3.5:9b") is False


def test_warm_from_profile_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warm-on-boot off must not call Ollama."""
    get_settings.cache_clear()
    monkeypatch.setenv("JARVIS_OLLAMA_WARM_ON_BOOT", "false")
    get_settings.cache_clear()
    called = MagicMock()
    monkeypatch.setattr("app.ollama_runtime.warm_ollama_model", called)
    warm_ollama_from_profile(
        Profile(chat_model="qwen3.5:9b", chat_provider=Provider.OLLAMA)
    )
    called.assert_not_called()
    get_settings.cache_clear()


def test_warm_from_profile_warms_chat_and_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled, warm chat + embed but never voice (avoids dual-load thrash)."""
    get_settings.cache_clear()
    monkeypatch.setenv("JARVIS_OLLAMA_WARM_ON_BOOT", "true")
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "30m")
    get_settings.cache_clear()
    warmed: list[str] = []

    def fake_warm(model: str, keep_alive: str | int | None = None) -> bool:
        """Record warmed model ids."""
        warmed.append(model)
        return True

    monkeypatch.setattr("app.ollama_runtime.warm_ollama_model", fake_warm)
    warm_ollama_from_profile(
        Profile(
            chat_model="qwen3.5:9b",
            chat_provider=Provider.OLLAMA,
            voice_model="qwen3.5:2b",
            voice_provider=Provider.OLLAMA,
            embedding_model="nomic-embed-text",
            embedding_provider=Provider.OLLAMA,
        )
    )
    assert warmed == ["qwen3.5:9b", "nomic-embed-text"]
    get_settings.cache_clear()
