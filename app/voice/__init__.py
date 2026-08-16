"""Fast voice agent: stream-first replies with vault + timer tools."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool, tool

from app.agent import build_chat_model
from app.models import (
    ChatMessage,
    DoneEvent,
    ErrorEvent,
    Profile,
    Provider,
    QueryMode,
    RetrievalStartEvent,
    StreamEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.monitoring import logger
from app.tts import strip_think_tags

VOICE_SYSTEM = """You are Jarvis, a concise voice assistant.
Answer in 1–3 short spoken sentences. Lead with the answer immediately.
Reply in the same language as the user unless they ask otherwise.
No markdown, lists, code, or XML. Never emit think tags or hidden reasoning.
Do not invent personal notes you have not been given.

You can use tools when needed:
- vault_search: look up facts in the user's Obsidian notes.
- timer_create: start a countdown. Convert the spoken delay to seconds_from_now
  exactly (30 seconds → 30, 1 minute → 60, 5 minutes → 300). Never invent a
  different duration.
- timer_list: list pending timers.
- timer_cancel: cancel a timer by id from timer_list.
When the user asks to start a timer or reminder, call timer_create — do not only say you will.
Confirm using the tool result duration only.
"""

# Cheap cue that the user wants vault facts — otherwise we stream immediately.
_VAULT_HINTS = (
    "vault",
    "obsidian",
    "my notes",
    "my note",
    "in my notes",
    "from my notes",
    "search my",
    "look up my",
    "what did i write",
    "what are my",
    "in the notes",
    "note about",
)

# Cue that the utterance needs timer tools (not the no-tool fast path).
_TIMER_HINTS = (
    "timer",
    "timers",
    "remind me",
    "reminder",
    "countdown",
    "set a timer",
    "start a timer",
    "start timer",
    "cancel timer",
    "cancel the timer",
    "list timers",
    "what timers",
    "alarm",
    "wake me",
    "in a minute",
    "in 1 minute",
    "in one minute",
    "minutes from now",
    "seconds from now",
)


def _likely_needs_vault(message: str) -> bool:
    """True when the utterance looks like a personal-notes lookup."""
    text = message.lower()
    return any(hint in text for hint in _VAULT_HINTS)


def _likely_needs_timer(message: str) -> bool:
    """True when the utterance looks like a timer/reminder request."""
    text = message.lower()
    return any(hint in text for hint in _TIMER_HINTS)


def _likely_needs_tools(message: str) -> bool:
    """True when the voice turn should bind tools instead of streaming blind."""
    return _likely_needs_vault(message) or _likely_needs_timer(message)


def _chunk_text(chunk: Any) -> str:
    """Normalize a streamed model chunk into plain text."""
    text = getattr(chunk, "content", None) or ""
    if isinstance(text, list):
        text = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in text
        )
    return strip_think_tags(str(text)) if text else ""


async def _search_vault(query: str, profile: Profile) -> str:
    """Run local hybrid retrieval and return a short speech-friendly summary."""
    from app.ingestion.dim_guard import check_embedding_compatibility
    from app.ingestion.embeddings import embed_query
    from app.retrieval.modes import retrieve

    compat = await check_embedding_compatibility(profile)
    if not compat.ok:
        return f"Vault search unavailable: {compat.error}"

    try:
        embedding = await embed_query(profile, query)
    except Exception as exc:  # noqa: BLE001
        return f"Vault search failed to embed query: {exc}"

    chunks: list[dict[str, Any]] = []
    async for item in retrieve(
        query, profile, embedding=embedding, mode=QueryMode.LOCAL
    ):
        if isinstance(item, list):
            chunks = item

    if not chunks:
        return "No matching notes found in the vault."

    lines: list[str] = []
    for chunk in chunks[:5]:
        title = chunk.get("note_title") or chunk.get("title") or "Note"
        path = chunk.get("note_path") or chunk.get("path") or ""
        text = (chunk.get("text") or chunk.get("content") or "").strip()
        if len(text) > 400:
            text = text[:400] + "…"
        lines.append(f"- {title} ({path}): {text}")
    return "Vault excerpts:\n" + "\n".join(lines)


# Default when profile.voice_model is blank (legacy JSON / hand-edited files).
_DEFAULT_VOICE_MODEL = "qwen3.5:2b"


def resolve_voice_model(profile: Profile) -> tuple[str, Provider]:
    """Return the LLM id/provider for voice only (never chat_model)."""
    model = (profile.voice_model or "").strip() or _DEFAULT_VOICE_MODEL
    provider = Provider(profile.voice_provider) if profile.voice_provider else Provider.OLLAMA
    return model, provider


def _history_messages(history: list[ChatMessage]) -> list[Any]:
    """Map chat history into LangChain messages (user/assistant only)."""
    out: list[Any] = []
    for item in history[-8:]:
        if item.role == "user":
            out.append(HumanMessage(content=item.content))
        elif item.role == "assistant":
            # Drop any leaked think traces from prior turns.
            out.append(AIMessage(content=strip_think_tags(item.content)))
    return out


async def _stream_tokens(model: Any, messages: list[Any]) -> AsyncIterator[StreamEvent]:
    """Yield spoken tokens as the model streams; never block on a full reply."""
    async for chunk in model.astream(messages):
        text = _chunk_text(chunk)
        if text:
            yield TokenEvent(text=text)


def _build_voice_tools(profile: Profile) -> list[Any]:
    """Return LangChain tools voice may call (vault + timers)."""
    from app.tools.timers import (
        cancel_timer_tool,
        create_timer_tool,
        list_timers_tool,
    )

    @tool
    async def vault_search(query: str) -> str:
        """Search the Obsidian vault for relevant note excerpts."""
        return await _search_vault(query, profile)

    timer_create = StructuredTool.from_function(
        coroutine=create_timer_tool,
        name="timer_create",
        description=(
            "Start a countdown timer. "
            "seconds_from_now is the delay in whole seconds "
            "(thirty seconds=30, one minute=60, two minutes=120)."
        ),
    )
    timer_list = StructuredTool.from_function(
        coroutine=list_timers_tool,
        name="timer_list",
        description="List pending timers with fire times and short ids.",
    )
    timer_cancel = StructuredTool.from_function(
        coroutine=cancel_timer_tool,
        name="timer_cancel",
        description="Cancel a pending timer by full id or short id prefix.",
    )
    return [vault_search, timer_create, timer_list, timer_cancel]


async def stream_voice(
    message: str,
    profile: Profile,
    history: list[ChatMessage] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream a voice turn; tools for vault/timers, else stream-first chat."""
    started = time.monotonic()
    message_id = uuid.uuid4().hex
    history = history or []
    voice_model, voice_provider = resolve_voice_model(profile)

    try:
        from app.config import get_settings

        # Smaller voice_model + reasoning=False: low TTFA, leave chat on Qwen.
        # Voice keep_alive is separate so chat↔voice swaps need not share pin policy.
        voice_keep = get_settings().ollama_voice_keep_alive
        model = build_chat_model(
            voice_model,
            voice_provider,
            max_context_tokens=min(profile.max_context_tokens or 4096, 4096),
            reasoning=False,
            keep_alive=voice_keep,
        )
    except Exception as exc:  # noqa: BLE001
        yield ErrorEvent(message=str(exc), code="voice_model_error", recoverable=False)
        yield DoneEvent(
            message_id=message_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return

    messages: list[Any] = [
        SystemMessage(content=VOICE_SYSTEM),
        *_history_messages(history),
        HumanMessage(content=message),
    ]

    # Fast path: conversation — tokens flow immediately (no tool round-trip).
    if not _likely_needs_tools(message):
        try:
            async for event in _stream_tokens(model, messages):
                yield event
        except Exception as stream_exc:  # noqa: BLE001
            yield ErrorEvent(
                message=str(stream_exc), code="voice_stream_error", recoverable=True
            )
        yield DoneEvent(
            message_id=message_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return

    tools = _build_voice_tools(profile)
    tool_map: dict[str, Any] = {t.name: t for t in tools}
    llm_tools = model.bind_tools(tools)

    try:
        first = await llm_tools.ainvoke(messages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Voice tool invoke failed (%s); streaming without tools", exc)
        try:
            async for event in _stream_tokens(model, messages):
                yield event
        except Exception as stream_exc:  # noqa: BLE001
            yield ErrorEvent(
                message=str(stream_exc), code="voice_stream_error", recoverable=True
            )
        yield DoneEvent(
            message_id=message_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return

    tool_calls = getattr(first, "tool_calls", None) or []
    if tool_calls:
        messages.append(first)
        for call in tool_calls:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
            call_id = (
                call.get("id")
                if isinstance(call, dict)
                else getattr(call, "id", uuid.uuid4().hex)
            )
            # Small voice models often copy tool-description examples (e.g. 300).
            # Prefer an unambiguous duration parsed from the user's words.
            call_args = dict(args or {})
            if name == "timer_create":
                from app.tools.timers import parse_spoken_delay_seconds

                spoken = parse_spoken_delay_seconds(message)
                if spoken is not None:
                    model_secs = call_args.get("seconds_from_now")
                    if model_secs != spoken:
                        logger.info(
                            "Timer delay override: model=%s spoken=%s utterance=%r",
                            model_secs,
                            spoken,
                            message[:120],
                        )
                    call_args["seconds_from_now"] = spoken

            yield ToolCallEvent(
                id=str(call_id),
                name=str(name),
                arguments=call_args,
            )
            if name == "vault_search":
                yield RetrievalStartEvent(
                    mode=QueryMode.LOCAL,
                    label="Voice vault search",
                    estimated_calls=1,
                )
            tool_fn = tool_map.get(str(name))
            if tool_fn is None:
                result = f"Unknown tool: {name}"
                ok = False
            else:
                try:
                    result = await tool_fn.ainvoke(call_args)
                    ok = True
                except Exception as exc:  # noqa: BLE001
                    result = f"Tool error: {exc}"
                    ok = False
            yield ToolResultEvent(
                id=str(call_id),
                name=str(name),
                ok=ok,
                result=str(result)[:2000],
            )
            messages.append(
                ToolMessage(content=str(result), tool_call_id=str(call_id))
            )

        try:
            async for event in _stream_tokens(model, messages):
                yield event
        except Exception as exc:  # noqa: BLE001
            yield ErrorEvent(message=str(exc), code="voice_stream_error", recoverable=True)
    else:
        content = strip_think_tags(getattr(first, "content", "") or "")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
            content = strip_think_tags(str(content))
        if content:
            yield TokenEvent(text=str(content))
        else:
            try:
                async for event in _stream_tokens(model, messages):
                    yield event
            except Exception as exc:  # noqa: BLE001
                yield ErrorEvent(
                    message=str(exc), code="voice_stream_error", recoverable=True
                )

    yield DoneEvent(
        message_id=message_id,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )
