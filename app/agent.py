"""Model registry and the placeholder retrieval engine.

The engine here streams from a real chat model but fabricates the retrieval
step, which is enough to build and exercise the whole UI -- streaming, progress,
cancellation, citations -- before the real pipeline exists. Replacing it means
implementing :class:`~app.models.RetrievalEngine` and nothing else.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import get_settings
from app.models import (
    ChatMessage,
    Citation,
    CitationsEvent,
    DoneEvent,
    ErrorEvent,
    IndexStatus,
    ModelInfo,
    Profile,
    Provider,
    QueryMode,
    RetrievalProgressEvent,
    RetrievalStartEvent,
    StreamEvent,
    TokenEvent,
)
from app.monitoring import get_metrics, logger, tracing
from app.security import get_policy_engine
from app.ingestion.chunkers import estimate_tokens

# Substrings that identify a vision-capable model. Visual retrieval returns page
# images, so this drives a hard validation error rather than a hint.
VISION_HINTS = ("vl", "vision", "llava", "gpt-4o", "gpt-4.1", "gemma3", "qwen3.5")
EMBED_HINTS = ("embed", "bge", "minilm", "e5-")

OPENAI_CHAT_MODELS: tuple[tuple[str, str, int, bool], ...] = (
    ("gpt-4o", "GPT-4o", 128_000, True),
    ("gpt-4o-mini", "GPT-4o mini", 128_000, True),
    ("gpt-4.1", "GPT-4.1", 1_000_000, True),
    ("gpt-4.1-mini", "GPT-4.1 mini", 1_000_000, True),
)

# Always listed in the picker. Discovery marks them available once pulled.
OLLAMA_CHAT_MODELS: tuple[tuple[str, str, int, bool], ...] = (
    ("qwen3.5:9b", "Qwen 3.5 9B", 262_144, True),
    ("qwen3.5:2b", "Qwen 3.5 2B", 262_144, True),
    ("qwen3:8b", "Qwen 3 8B", 40_960, False),
    ("qwen2.5:3b", "Qwen 2.5 3B", 32_768, False),
)

OPENAI_EMBED_MODELS: tuple[tuple[str, str, int], ...] = (
    ("text-embedding-3-small", "text-embedding-3-small", 1536),
    ("text-embedding-3-large", "text-embedding-3-large", 3072),
)

OLLAMA_EMBED_MODELS: tuple[tuple[str, str, int], ...] = (
    ("qwen3-embedding:8b", "Qwen3 Embedding 8B", 4096),
    ("nomic-embed-text", "nomic-embed-text", 768),
    ("mxbai-embed-large", "mxbai-embed-large", 1024),
    ("all-minilm", "all-minilm", 384),
    ("bge-m3", "bge-m3", 1024),
)

OLLAMA_EMBED_DIMENSIONS = {
    "qwen3-embedding": 4096,
    "qwen3-embedding:8b": 4096,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
    "bge-m3": 1024,
}


class ModelRegistry:
    """Discovers what is actually runnable right now."""

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        """Cache discovered models for ``ttl_seconds`` under an asyncio lock."""
        self._ttl = ttl_seconds
        self._cache: dict[str, ModelInfo] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def all(self, *, force: bool = False) -> dict[str, ModelInfo]:
        """Return chat and embedding models, refreshing when the TTL expires."""
        async with self._lock:
            fresh = time.monotonic() - self._fetched_at < self._ttl
            if self._cache is not None and fresh and not force:
                return self._cache

            models: dict[str, ModelInfo] = {}
            for info in await self._ollama_models():
                models[info.id] = info
            for info in self._curated_ollama_models(models):
                models[info.id] = info
            for info in self._openai_models():
                models[info.id] = info

            from app.models.recommend import merge_catalog_into_registry

            models = merge_catalog_into_registry(models)
            await self._enrich_ollama_show(models)

            self._cache = models
            self._fetched_at = time.monotonic()
            return models

    async def _ollama_models(self) -> list[ModelInfo]:
        """List models from the local Ollama tags API; empty if unreachable."""
        settings = get_settings()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{settings.ollama_base_url}/api/tags")
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Ollama unreachable at %s (%s)", settings.ollama_base_url, exc)
            return []

        found: list[ModelInfo] = []
        for entry in payload.get("models", []):
            name = entry.get("name", "")
            if not name:
                continue
            lowered = name.lower()
            is_embedding = any(hint in lowered for hint in EMBED_HINTS)
            base = name.split(":")[0]
            details = entry.get("details") or {}
            dims = None
            if is_embedding:
                dims = (
                    OLLAMA_EMBED_DIMENSIONS.get(name)
                    or OLLAMA_EMBED_DIMENSIONS.get(base)
                    or _coerce_positive_int(details.get("embedding_length"))
                )
            found.append(
                ModelInfo(
                    id=name,
                    provider=Provider.OLLAMA,
                    label=name,
                    context_window=int(
                        details.get("context_length") or 8192
                    ),
                    supports_vision=any(hint in lowered for hint in VISION_HINTS),
                    supports_tools=not is_embedding,
                    is_embedding=is_embedding,
                    dimensions=dims,
                    available=True,
                )
            )
        return found

    def _curated_ollama_models(
        self, discovered: dict[str, ModelInfo]
    ) -> list[ModelInfo]:
        """Ensure known Ollama chat/embed models appear even before they are pulled."""
        found: list[ModelInfo] = []
        for model_id, label, window, vision in OLLAMA_CHAT_MODELS:
            if model_id in discovered:
                continue
            found.append(
                ModelInfo(
                    id=model_id,
                    provider=Provider.OLLAMA,
                    label=label,
                    context_window=window,
                    supports_vision=vision,
                    available=False,
                    unavailable_reason=f"Run: ollama pull {model_id}",
                )
            )
        for model_id, label, dims in OLLAMA_EMBED_MODELS:
            if model_id in discovered or any(
                d.id == model_id or d.id.startswith(f"{model_id}:")
                for d in discovered.values()
            ):
                continue
            found.append(
                ModelInfo(
                    id=model_id,
                    provider=Provider.OLLAMA,
                    label=label,
                    is_embedding=True,
                    supports_tools=False,
                    dimensions=dims,
                    available=False,
                    unavailable_reason=f"Run: ollama pull {model_id}",
                )
            )
        return found

    async def _enrich_ollama_show(self, models: dict[str, ModelInfo]) -> None:
        """Best-effort ``/api/show`` for pulled Ollama models missing size/params."""
        settings = get_settings()
        missing = [
            info
            for info in models.values()
            if info.provider is Provider.OLLAMA
            and info.available
            and (
                info.size_bytes is None
                or info.parameter_b is None
                or (info.is_embedding and info.dimensions is None)
            )
        ]
        if not missing:
            return
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                for info in missing[:12]:
                    try:
                        response = await client.post(
                            f"{settings.ollama_base_url}/api/show",
                            json={"name": info.id},
                        )
                        response.raise_for_status()
                        payload = response.json()
                    except (httpx.HTTPError, ValueError):
                        continue
                    details = payload.get("details") or {}
                    model_info = payload.get("model_info") or {}
                    updates: dict = {}
                    if info.size_bytes is None:
                        size = payload.get("size")
                        if isinstance(size, int):
                            updates["size_bytes"] = size
                    if info.parameter_b is None:
                        param = details.get("parameter_size") or details.get(
                            "parameter_size_str"
                        )
                        parsed = _parse_parameter_b(param)
                        if parsed is not None:
                            updates["parameter_b"] = parsed
                    if info.is_embedding and info.dimensions is None:
                        dims = _embedding_length_from_show(details, model_info)
                        if dims is not None:
                            updates["dimensions"] = dims
                    if updates:
                        models[info.id] = info.model_copy(update=updates)
        except httpx.HTTPError:
            return

    def _openai_models(self) -> list[ModelInfo]:
        """Curated OpenAI chat/embed list; marked unavailable without an API key."""
        settings = get_settings()
        has_key = settings.resolved_openai_api_key() is not None
        reason = None if has_key else "Set JARVIS_OPENAI_API_KEY to use OpenAI models."

        models = [
            ModelInfo(
                id=model_id,
                provider=Provider.OPENAI,
                label=label,
                context_window=window,
                supports_vision=vision,
                available=has_key,
                unavailable_reason=reason,
            )
            for model_id, label, window, vision in OPENAI_CHAT_MODELS
        ]
        models += [
            ModelInfo(
                id=model_id,
                provider=Provider.OPENAI,
                label=label,
                is_embedding=True,
                supports_tools=False,
                dimensions=dims,
                available=has_key,
                unavailable_reason=reason,
            )
            for model_id, label, dims in OPENAI_EMBED_MODELS
        ]
        return models


def _parse_parameter_b(raw: object) -> float | None:
    """Parse Ollama parameter_size strings like ``8.0B`` into billions."""
    if raw is None:
        return None
    text = str(raw).strip().upper().replace(" ", "")
    if not text:
        return None
    try:
        if text.endswith("B"):
            return float(text[:-1])
        if text.endswith("M"):
            return float(text[:-1]) / 1000.0
        return float(text)
    except ValueError:
        return None


def _coerce_positive_int(raw: object) -> int | None:
    """Return a positive int from Ollama metadata, or None."""
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _embedding_length_from_show(
    details: dict, model_info: dict
) -> int | None:
    """Read embedding width from Ollama ``/api/show`` details or model_info."""
    direct = _coerce_positive_int(details.get("embedding_length"))
    if direct is not None:
        return direct
    for key, value in model_info.items():
        if str(key).endswith("embedding_length"):
            dims = _coerce_positive_int(value)
            if dims is not None:
                return dims
    return None


def build_chat_model(
    model: str,
    provider: Provider,
    *,
    max_context_tokens: int | None = None,
    reasoning: bool | None = None,
    keep_alive: str | int | None = None,
):
    """Instantiate a LangChain chat model for ``model``/``provider``.

    Callers pass the role-specific ids (chat, voice, extraction, rerank,
    decision) rather than a whole :class:`Profile`, so those roles can disagree.
    ``max_context_tokens`` is only applied for Ollama (``num_ctx``).
    ``reasoning`` maps to Ollama thinking mode (``False`` disables ``<think>``
    traces — required for low-latency voice).
    ``keep_alive`` overrides ``JARVIS_OLLAMA_KEEP_ALIVE`` for this call when set
    (voice uses ``JARVIS_OLLAMA_VOICE_KEEP_ALIVE``); blank means Ollama default.

    In demo mode, OpenAI chat uses only the per-request BYOK from
    :mod:`app.llm_session` (never ``JARVIS_OPENAI_API_KEY``). Desktop may still
    fall back to the process env key when no request key is bound.
    """
    settings = get_settings()
    # Bound hung providers so chat/reindex cannot stick the event loop forever.
    timeout_s = 120.0
    if provider is Provider.OPENAI:
        from langchain_openai import ChatOpenAI

        from app.llm_session import get_request_llm_api_key, get_request_llm_base_url

        request_key = get_request_llm_api_key()
        if settings.demo_mode:
            api_key = request_key
            if api_key is None:
                msg = (
                    "Demo mode requires a session OpenAI-compatible API key "
                    "(X-Jarvis-User-LLM-Key)."
                )
                raise RuntimeError(msg)
        else:
            api_key = request_key or settings.resolved_openai_api_key()
            if api_key is None:
                msg = "No OpenAI API key configured."
                raise RuntimeError(msg)
        kwargs: dict = {
            "model": model,
            "api_key": api_key,
            "streaming": True,
            "request_timeout": timeout_s,
        }
        base_url = get_request_llm_base_url()
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    from langchain_ollama import ChatOllama

    from app.ollama_runtime import normalize_keep_alive

    kwargs: dict = {
        "model": model,
        "base_url": settings.ollama_base_url,
        "client_kwargs": {"timeout": timeout_s},
    }
    if max_context_tokens is not None:
        kwargs["num_ctx"] = max_context_tokens
    if reasoning is not None:
        kwargs["reasoning"] = reasoning
    resolved_keep = normalize_keep_alive(
        keep_alive if keep_alive is not None else settings.ollama_keep_alive
    )
    if resolved_keep is not None:
        kwargs["keep_alive"] = resolved_keep
    return ChatOllama(**kwargs)


class PlaceholderRetrievalEngine:
    """Streams a real model answer over stand-in retrieval.

    Broad mode deliberately emits a per-community progress sequence, because
    the real thing runs one LLM call per community report and the UI has to
    prove it can render that before the pipeline lands.
    """

    name = "placeholder"

    def __init__(self, registry: ModelRegistry) -> None:
        """Hold the model registry used when streaming answers."""
        self.registry = registry

    async def index_status(self) -> IndexStatus:
        """Report vault note count; ``ready`` stays False until a real index exists."""
        policy = get_policy_engine()
        vault = policy.vault_path
        total = 0
        if vault and vault.exists():
            total = sum(1 for _ in vault.rglob("*.md"))
        return IndexStatus(
            engine=self.name,
            ready=False,
            total_notes=total,
            last_indexed_at=None,
        )

    async def query(
        self,
        question: str,
        profile: Profile,
        history: list[ChatMessage] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream fake retrieval, citations, then real tokens; re-raise cancel.

        On cancellation, :class:`DoneEvent` is skipped so the client can treat
        disconnect as an aborted run rather than a completed one.
        """
        started = time.monotonic()
        message_id = uuid.uuid4().hex
        cancelled = False

        try:
            async for event in self._retrieve(profile):
                yield event

            citations = self._citations(question)
            if citations:
                yield CitationsEvent(citations=citations)

            async for event in self._generate(question, profile, history, citations):
                yield event

        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:  # noqa: BLE001 - logged server-side; client gets a safe message
            logger.exception("Query failed")
            yield ErrorEvent(
                message="Query failed. Check the backend logs for details.",
                code="query_failed",
            )
        finally:
            if not cancelled:
                yield DoneEvent(
                    message_id=message_id,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )

    async def _retrieve(self, profile: Profile) -> AsyncIterator[StreamEvent]:
        """Emit staged retrieval progress events (sleeps approximate real work)."""
        if profile.query_mode is QueryMode.GLOBAL:
            total = 8 + profile.community_level * 4
            yield RetrievalStartEvent(
                mode=profile.query_mode,
                label=f"Summarising {total} communities at level {profile.community_level}",
                estimated_calls=total + 1,
                estimated_seconds=total * 1.5,
            )
            for i in range(1, total + 1):
                await asyncio.sleep(0.12)
                yield RetrievalProgressEvent(
                    current=i, total=total, label=f"Community {i} of {total}"
                )
        else:
            yield RetrievalStartEvent(
                mode=profile.query_mode,
                label="Finding entry points and traversing the graph",
                estimated_calls=1,
                estimated_seconds=2.0,
            )
            for i in range(1, 4):
                await asyncio.sleep(0.1)
                yield RetrievalProgressEvent(
                    current=i,
                    total=3,
                    label=("Embedding query", "Matching entities", "Expanding neighbours")[i - 1],
                )

    def _citations(self, question: str) -> list[Citation]:
        """Cite vault notes that pass PolicyEngine path checks (placeholder path)."""
        del question  # Placeholder ranking does not use the query text yet.
        policy = get_policy_engine()
        vault = policy.vault_path
        if not vault or not vault.exists():
            return []

        notes: list[Path] = []
        for path in vault.rglob("*.md"):
            if any(part.startswith(".") for part in path.relative_to(vault).parts):
                continue
            verdict = policy.check("vault_read", path=path, mode="read")
            if not verdict.allowed:
                continue
            notes.append(path)
            if len(notes) >= 3:
                break

        citations: list[Citation] = []
        for rank, path in enumerate(notes):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            snippet = text[:280].strip()
            citations.append(
                Citation(
                    id=uuid.uuid4().hex,
                    note_path=str(path.relative_to(vault)).replace("\\", "/"),
                    note_title=path.stem,
                    heading_path=_first_heading(text),
                    snippet=snippet,
                    char_start=0,
                    char_end=len(snippet),
                    score=round(0.92 - rank * 0.11, 3),
                    source="graph",
                )
            )
        return citations

    async def _generate(
        self,
        question: str,
        profile: Profile,
        history: list[ChatMessage] | None,
        citations: list[Citation],
    ) -> AsyncIterator[StreamEvent]:
        """Stream chat tokens with optional LangSmith tracing around the call."""
        policy = get_policy_engine()
        context = "\n\n".join(
            f"[{c.note_title}]\n{c.snippet}" for c in citations
        ) or "(no notes indexed yet)"

        from app.retrieval.prompts import build_rag_chat_messages

        messages = build_rag_chat_messages(
            policy_text=policy.system_prompt(),
            retrieved_context=context,
            question=question,
            history=history,
        )
        # Placeholder engine uses LangChain tuple messages in some paths; adapt.
        tuple_messages: list[tuple[str, str]] = [
            (str(m["role"]), str(m["content"])) for m in messages
        ]

        try:
            model = build_chat_model(
                profile.chat_model,
                profile.chat_provider,
                max_context_tokens=profile.max_context_tokens,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not start chat model %s", profile.chat_model)
            yield ErrorEvent(
                message=(
                    f"Could not start {profile.chat_model}. "
                    "Check provider settings and that the model is available."
                ),
                code="model_unavailable",
            )
            return

        with tracing(profile.tracing_enabled):
            try:
                assembled: list[str] = []
                async for chunk in model.astream(tuple_messages):
                    text = getattr(chunk, "content", "") or ""
                    if isinstance(text, list):
                        text = "".join(
                            part.get("text", "")
                            for part in text
                            if isinstance(part, dict)
                        )
                    if text:
                        assembled.append(text)
                        yield TokenEvent(text=text)
                prompt_tokens = sum(
                    estimate_tokens(content) for _role, content in tuple_messages
                )
                get_metrics().record_tokens(
                    input_tokens=prompt_tokens,
                    output_tokens=estimate_tokens("".join(assembled)),
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Chat generation failed for %s", profile.chat_model)
                yield ErrorEvent(
                    message=(
                        f"{profile.chat_model} failed. "
                        "Is Ollama running and the model pulled?"
                    ),
                    code="generation_failed",
                )


def _first_heading(text: str) -> list[str]:
    """Return the first ATX heading as a one-element path, or empty."""
    for line in text.splitlines():
        if line.startswith("#"):
            return [line.lstrip("#").strip()]
    return []


_registry: ModelRegistry | None = None


def get_registry() -> ModelRegistry:
    """Return the process-wide model registry, creating it on first use."""
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry
