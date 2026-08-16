"""Embedding helper tests with mocked clients."""

from __future__ import annotations

from typing import Any

import pytest

from app.models import Profile, Provider
from app.ingestion import embeddings as emb_mod


class _AsyncStub:
    """Embeddings client that only exposes async embed methods."""

    async def aembed_query(self, text: str) -> list[float]:
        """Return a vector derived from text length."""
        return [float(len(text)), 1.0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed each document independently."""
        return [await self.aembed_query(t) for t in texts]


class _SyncStub:
    """Embeddings client that only exposes sync embed methods."""

    def embed_query(self, text: str) -> list[float]:
        """Return a sync vector for the query."""
        return [float(len(text)), 2.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents synchronously."""
        return [self.embed_query(t) for t in texts]


@pytest.mark.asyncio
async def test_embed_query_uses_async_client(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emb_mod, "build_embeddings", lambda *_a, **_k: _AsyncStub())
    vector = await emb_mod.embed_query(profile, "abcd")
    assert vector == [4.0, 1.0]


@pytest.mark.asyncio
async def test_embed_documents_empty(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emb_mod, "build_embeddings", lambda *_a, **_k: _AsyncStub())
    assert await emb_mod.embed_documents(profile, []) == []


@pytest.mark.asyncio
async def test_embed_documents_preserves_order(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emb_mod, "build_embeddings", lambda *_a, **_k: _SyncStub())
    texts = ["a", "bb", "ccc"]
    vectors = await emb_mod.embed_documents(profile, texts)
    assert len(vectors) == 3
    assert vectors[0][0] == 1.0
    assert vectors[1][0] == 2.0
    assert vectors[2][0] == 3.0


def test_build_embeddings_openai_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            calls.update(kwargs)

    monkeypatch.setattr(
        "langchain_openai.OpenAIEmbeddings",
        FakeOpenAI,
        raising=False,
    )
    # Patch the import site used inside build_embeddings via module injection.
    import langchain_openai

    monkeypatch.setattr(langchain_openai, "OpenAIEmbeddings", FakeOpenAI)

    from app.config import get_settings

    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", "sk-embed-test")
    get_settings.cache_clear()
    try:
        client = emb_mod.build_embeddings("text-embedding-3-small", Provider.OPENAI)
        assert isinstance(client, FakeOpenAI)
        assert calls["model"] == "text-embedding-3-small"
        assert calls["api_key"] == "sk-embed-test"
        assert calls["request_timeout"] == emb_mod._EMBED_TIMEOUT_S
    finally:
        get_settings.cache_clear()


def test_build_embeddings_openai_blank_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank OpenAI keys must raise rather than pass api_key=None."""
    from app.config import get_settings

    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="No OpenAI API key configured"):
            emb_mod.build_embeddings("text-embedding-3-small", Provider.OPENAI)
    finally:
        get_settings.cache_clear()


def test_build_embeddings_ollama_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama embed path forwards model, timeout, and keep_alive seconds."""
    from app.config import get_settings

    calls: dict[str, Any] = {}

    class FakeOllama:
        def __init__(self, **kwargs: Any) -> None:
            """Record constructor kwargs for assertions."""
            calls.update(kwargs)

    import langchain_ollama

    monkeypatch.setattr(langchain_ollama, "OllamaEmbeddings", FakeOllama)
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "30m")
    get_settings.cache_clear()
    try:
        client = emb_mod.build_embeddings("nomic-embed-text", Provider.OLLAMA)
        assert isinstance(client, FakeOllama)
        assert calls["model"] == "nomic-embed-text"
        assert "base_url" in calls
        assert calls["client_kwargs"]["timeout"] == emb_mod._EMBED_TIMEOUT_S
        assert calls["keep_alive"] == 1800
    finally:
        get_settings.cache_clear()
