"""LangSmith tracing env scrubbing."""

from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.monitoring import tracing


_LANGSMITH_KEYS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGSMITH_PROJECT",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
)


def test_tracing_true_restores_langsmith_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tracing(True) must restore all LangSmith keys after the with-block."""
    monkeypatch.setenv("JARVIS_LANGSMITH_TRACING_V2", "true")
    monkeypatch.setenv("JARVIS_LANGSMITH_PROJECT", "jarvis-test")
    monkeypatch.setenv("JARVIS_LANGSMITH_API_KEY", "ls-test-key")
    monkeypatch.setenv(
        "JARVIS_LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
    )
    get_settings.cache_clear()

    prior = {
        "LANGSMITH_TRACING": "prior-trace",
        "LANGSMITH_TRACING_V2": "prior-v2",
        "LANGSMITH_PROJECT": "prior-project",
        "LANGSMITH_API_KEY": "prior-key",
        "LANGSMITH_ENDPOINT": "https://prior.example",
    }
    for key, value in prior.items():
        monkeypatch.setenv(key, value)

    try:
        with tracing(True):
            assert os.environ.get("LANGSMITH_TRACING") == "true"
            assert os.environ.get("LANGSMITH_TRACING_V2") == "true"
            assert os.environ.get("LANGSMITH_PROJECT") == "jarvis-test"
            assert os.environ.get("LANGSMITH_API_KEY") == "ls-test-key"
            assert (
                os.environ.get("LANGSMITH_ENDPOINT")
                == "https://api.smith.langchain.com"
            )

        for key, value in prior.items():
            assert os.environ.get(key) == value
    finally:
        get_settings.cache_clear()


def test_tracing_true_restores_previously_unset_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keys that were unset before tracing(True) must be popped again after."""
    monkeypatch.setenv("JARVIS_LANGSMITH_TRACING_V2", "true")
    monkeypatch.setenv("JARVIS_LANGSMITH_PROJECT", "jarvis-test")
    monkeypatch.setenv("JARVIS_LANGSMITH_API_KEY", "ls-test-key")
    get_settings.cache_clear()

    for key in _LANGSMITH_KEYS:
        monkeypatch.delenv(key, raising=False)

    try:
        with tracing(True):
            assert os.environ.get("LANGSMITH_TRACING") == "true"
            assert os.environ.get("LANGSMITH_API_KEY") == "ls-test-key"
            assert os.environ.get("LANGSMITH_ENDPOINT")

        for key in _LANGSMITH_KEYS:
            assert key not in os.environ
    finally:
        get_settings.cache_clear()


def test_tracing_false_restores_langsmith_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled path also restores snapshotted LangSmith env values."""
    get_settings.cache_clear()
    monkeypatch.setenv("LANGSMITH_TRACING", "ambient")
    monkeypatch.setenv("LANGSMITH_TRACING_V2", "ambient-v2")
    monkeypatch.setenv("LANGSMITH_PROJECT", "ambient-project")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ambient-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://ambient.example")

    with tracing(False):
        assert "LANGSMITH_TRACING" not in os.environ
        assert "LANGSMITH_TRACING_V2" not in os.environ

    assert os.environ.get("LANGSMITH_TRACING") == "ambient"
    assert os.environ.get("LANGSMITH_TRACING_V2") == "ambient-v2"
    assert os.environ.get("LANGSMITH_PROJECT") == "ambient-project"
    assert os.environ.get("LANGSMITH_API_KEY") == "ambient-key"
    assert os.environ.get("LANGSMITH_ENDPOINT") == "https://ambient.example"


def test_tracing_true_without_api_key_stays_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process+profile flags without a key must not arm LANGSMITH_TRACING."""
    monkeypatch.setenv("JARVIS_LANGSMITH_TRACING_V2", "true")
    monkeypatch.setenv("JARVIS_LANGSMITH_PROJECT", "jarvis-test")
    # Empty string overrides .env; delenv alone still loads the file key.
    monkeypatch.setenv("JARVIS_LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING_V2", raising=False)

    try:
        with tracing(True):
            assert "LANGSMITH_TRACING" not in os.environ
            assert "LANGSMITH_TRACING_V2" not in os.environ
    finally:
        get_settings.cache_clear()
