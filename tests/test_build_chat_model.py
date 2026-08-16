"""Regression: build_chat_model must accept (model, provider) like hybrid/reindex."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.agent import build_chat_model
from app.models import Profile, Provider


def test_build_chat_model_signature_binds_model_and_provider() -> None:
    """Call sites pass (model, provider); a Profile-only API would TypeError."""
    sig = inspect.signature(build_chat_model)
    sig.bind("qwen3:8b", Provider.OLLAMA)
    sig.bind("qwen2.5:3b", Provider.OLLAMA, max_context_tokens=4096)
    sig.bind("gpt-4o-mini", Provider.OPENAI)


def test_build_chat_model_rejects_profile_as_sole_positional() -> None:
    """Passing a Profile alone must fail — that old signature broke hybrid callers."""
    with pytest.raises(TypeError):
        build_chat_model(Profile())  # type: ignore[arg-type]


def test_build_chat_model_ollama_passes_model_and_num_ctx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ollama path must forward model id, optional context window, and keep_alive."""
    from app.config import get_settings

    captured: dict[str, Any] = {}

    class FakeChatOllama:
        def __init__(self, **kwargs: Any) -> None:
            """Record constructor kwargs for assertions."""
            captured.update(kwargs)

    import langchain_ollama

    monkeypatch.setattr(langchain_ollama, "ChatOllama", FakeChatOllama)
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "30m")
    get_settings.cache_clear()
    try:
        model = build_chat_model("rerank-model", Provider.OLLAMA, max_context_tokens=2048)
        assert isinstance(model, FakeChatOllama)
        assert captured["model"] == "rerank-model"
        assert captured["num_ctx"] == 2048
        assert captured["keep_alive"] == "30m"
        assert captured["client_kwargs"]["timeout"] == 120.0
    finally:
        get_settings.cache_clear()


def test_build_chat_model_ollama_keep_alive_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit keep_alive overrides the process default (voice path)."""
    from app.config import get_settings

    captured: dict[str, Any] = {}

    class FakeChatOllama:
        def __init__(self, **kwargs: Any) -> None:
            """Record constructor kwargs for assertions."""
            captured.update(kwargs)

    import langchain_ollama

    monkeypatch.setattr(langchain_ollama, "ChatOllama", FakeChatOllama)
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "30m")
    get_settings.cache_clear()
    try:
        build_chat_model("qwen3.5:2b", Provider.OLLAMA, keep_alive="10m")
        assert captured["keep_alive"] == "10m"
    finally:
        get_settings.cache_clear()


def test_build_chat_model_ollama_blank_keep_alive_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty keep_alive must omit the kwarg so Ollama uses its own default."""
    from app.config import get_settings

    captured: dict[str, Any] = {}

    class FakeChatOllama:
        def __init__(self, **kwargs: Any) -> None:
            """Record constructor kwargs for assertions."""
            captured.update(kwargs)

    import langchain_ollama

    monkeypatch.setattr(langchain_ollama, "ChatOllama", FakeChatOllama)
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "")
    get_settings.cache_clear()
    try:
        build_chat_model("qwen3.5:9b", Provider.OLLAMA)
        assert "keep_alive" not in captured
    finally:
        get_settings.cache_clear()

def test_build_chat_model_openai_blank_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank OpenAI keys must raise rather than fall back to ambient env."""
    from app.config import get_settings

    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", "   ")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="No OpenAI API key configured"):
        build_chat_model("gpt-4o-mini", Provider.OPENAI)
    get_settings.cache_clear()


def test_build_chat_model_openai_passes_resolved_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI path must pass the configured key explicitly into ChatOpenAI."""
    from app.config import get_settings

    captured: dict[str, Any] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            """Record constructor kwargs for assertions."""
            captured.update(kwargs)

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", "sk-test-key")
    get_settings.cache_clear()
    try:
        model = build_chat_model("gpt-4o-mini", Provider.OPENAI)
        assert isinstance(model, FakeChatOpenAI)
        assert captured["model"] == "gpt-4o-mini"
        assert captured["api_key"] == "sk-test-key"
        assert captured["request_timeout"] == 120.0
    finally:
        get_settings.cache_clear()


def test_role_specific_call_sites_match_signature(profile: Profile) -> None:
    """Hybrid/reindex/rerank call patterns must bind without TypeError."""
    sig = inspect.signature(build_chat_model)
    patterns = [
        (profile.chat_model, Provider(profile.chat_provider)),
        (profile.extraction_model, Provider(profile.extraction_provider)),
        (profile.rerank_model, Provider(profile.rerank_provider)),
        (profile.chunk_decision_model, Provider(profile.chunk_decision_provider)),
        (
            profile.chat_model,
            Provider(profile.chat_provider),
        ),
    ]
    for model, provider in patterns:
        sig.bind(model, provider)
    sig.bind(
        profile.chat_model,
        Provider(profile.chat_provider),
        max_context_tokens=profile.max_context_tokens,
    )
