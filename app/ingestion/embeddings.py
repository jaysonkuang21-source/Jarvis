"""Embedding helpers for index and query time."""

from __future__ import annotations

from typing import Sequence

from app.config import get_settings
from app.models import Profile, Provider

# Bound hung embed providers so probes / reindex cannot hang forever.
_EMBED_TIMEOUT_S = 60.0


def build_embeddings(model: str, provider: Provider):
    """Construct a LangChain embeddings client for Ollama or OpenAI.

    OpenAI refuses blank/missing keys rather than passing ``api_key=None``,
    which would let LangChain fall back to ambient ``OPENAI_API_KEY``.
    Ollama clients inherit ``JARVIS_OLLAMA_KEEP_ALIVE`` (as seconds) so embed
    models are not unloaded mid-session.
    """
    if provider is Provider.OPENAI:
        from langchain_openai import OpenAIEmbeddings

        settings = get_settings()
        key = settings.resolved_openai_api_key()
        if key is None:
            msg = "No OpenAI API key configured."
            raise RuntimeError(msg)
        return OpenAIEmbeddings(
            model=model,
            api_key=key,
            request_timeout=_EMBED_TIMEOUT_S,
        )
    from langchain_ollama import OllamaEmbeddings

    from app.ollama_runtime import keep_alive_as_seconds

    settings = get_settings()
    kwargs: dict = {
        "model": model,
        "base_url": settings.ollama_base_url,
        "client_kwargs": {"timeout": _EMBED_TIMEOUT_S},
    }
    # OllamaEmbeddings only accepts int seconds / -1 (not duration strings).
    keep = keep_alive_as_seconds(settings.ollama_keep_alive)
    if keep is not None:
        kwargs["keep_alive"] = keep
    return OllamaEmbeddings(**kwargs)


async def embed_query(profile: Profile, text: str) -> list[float]:
    """Embed a single query string and record input tokens."""
    from app.ingestion.chunkers import estimate_tokens
    from app.monitoring import get_metrics

    get_metrics().record_tokens(input_tokens=estimate_tokens(text))
    emb = build_embeddings(profile.embedding_model, profile.embedding_provider)
    if hasattr(emb, "aembed_query"):
        return list(await emb.aembed_query(text))
    return list(emb.embed_query(text))


async def embed_documents(profile: Profile, texts: Sequence[str]) -> list[list[float]]:
    """Embed many documents and record their combined input tokens."""
    if not texts:
        return []
    from app.ingestion.chunkers import estimate_tokens
    from app.monitoring import get_metrics

    get_metrics().record_tokens(
        input_tokens=sum(estimate_tokens(t) for t in texts),
    )
    emb = build_embeddings(profile.embedding_model, profile.embedding_provider)
    if hasattr(emb, "aembed_documents"):
        return [list(v) for v in await emb.aembed_documents(list(texts))]
    return [list(v) for v in emb.embed_documents(list(texts))]
