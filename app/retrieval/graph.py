"""LangGraph workflow for hybrid retrieval + agentic grade/rewrite + generate.

Orchestrates existing `modes.retrieve` (Local hybrid RRF / Global / DRIFT) and
grade→rewrite loops. Streams `StreamEvent`s via LangGraph custom stream mode
so `/api/chat` SSE keeps progress, citations, and tokens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Optional
from typing_extensions import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from app.agent import build_chat_model
from app.ingestion.chunkers import estimate_tokens
from app.ingestion.embeddings import embed_query
from app.models import (
    ChatMessage,
    CitationsEvent,
    ErrorEvent,
    Profile,
    Provider,
    QueryMode,
    RagMode,
    RetrievalProgressEvent,
    RetrievalStartEvent,
    StreamEvent,
    StreamEventAdapter,
    TokenEvent,
)
from app.monitoring import logger
from app.retrieval.expand import chunk_to_citation, expand_chunks
from app.retrieval.modes import resolve_mode, retrieve as retrieve_mode
from app.retrieval.prompts import build_rag_chat_messages
from app.retrieval.rerank import grade_relevant, rewrite_query
from app.security import get_policy_engine

# One primary attempt + one same-model retry before the error node.
_MAX_GENERATE_ATTEMPTS = 2

_MODE_LABELS = {
    QueryMode.LOCAL: "Local hybrid search",
    QueryMode.GLOBAL: "Global community map-reduce",
    QueryMode.DRIFT: "DRIFT search",
    QueryMode.AUTO: "Auto-routed search",
}


class RetrievalState(TypedDict, total=False):
    """LangGraph state for one chat retrieval + generation turn."""

    question: str
    query: str
    history: list[ChatMessage]
    profile: Profile
    mode: QueryMode
    rag_mode: RagMode
    chunks: list[dict[str, Any]]
    attempt: int
    max_iters: int
    relevant: bool
    generate_attempt: int
    error: Optional[str]
    model_used: str
    generation_ok: bool
    assembled: str
    prompt_messages: list[dict[str, str]]
    retrieval_failed: bool


def route_after_retrieve(state: RetrievalState) -> Literal["grade", "expand"]:
    """After retrieve: agentic grades evidence; regular skips to expand."""
    if state.get("rag_mode") is RagMode.AGENTIC:
        return "grade"
    return "expand"


def route_after_grade(state: RetrievalState) -> Literal["expand", "rewrite"]:
    """Grade YES or exhausted/empty → expand; else rewrite and retrieve again."""
    if state.get("relevant"):
        return "expand"
    chunks = state.get("chunks") or []
    if not chunks:
        return "expand"
    attempt = int(state.get("attempt") or 1)
    max_iters = int(state.get("max_iters") or 1)
    if attempt < max_iters:
        return "rewrite"
    return "expand"


def route_after_citations(state: RetrievalState) -> Literal["generate", "no_docs"]:
    """Skip generation when retrieval returned nothing."""
    if state.get("chunks"):
        return "generate"
    return "no_docs"


def route_after_generate(state: RetrievalState) -> Literal["done", "retry", "error"]:
    """Primary success ends; one retry then graceful error node."""
    if state.get("generation_ok"):
        return "done"
    if int(state.get("generate_attempt") or 0) < _MAX_GENERATE_ATTEMPTS:
        return "retry"
    return "error"


def _emit(event: StreamEvent) -> None:
    """Push a StreamEvent onto the LangGraph custom stream when available."""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(event)


async def node_resolve_mode(state: RetrievalState) -> dict[str, Any]:
    """Resolve Auto/Local/Global/DRIFT and emit RetrievalStartEvent."""
    profile = state["profile"]
    question = state["question"]
    mode = await resolve_mode(question, profile)
    label = _MODE_LABELS.get(mode, "Retrieval")
    _emit(RetrievalStartEvent(mode=mode, label=label, estimated_calls=1))
    return {
        "mode": mode,
        "query": question,
        "attempt": 0,
        "max_iters": int(profile.agentic_max_iters),
        "rag_mode": profile.rag_mode,
        "generate_attempt": 0,
        "generation_ok": False,
        "error": None,
        "chunks": [],
    }


async def node_retrieve(state: RetrievalState) -> dict[str, Any]:
    """Run hybrid/mode retrieval for the current query; stream progress events."""
    profile = state["profile"]
    mode = state["mode"]
    query = state.get("query") or state["question"]
    attempt = int(state.get("attempt") or 0) + 1
    max_iters = int(state.get("max_iters") or 1)

    if state.get("rag_mode") is RagMode.AGENTIC:
        _emit(
            RetrievalProgressEvent(
                current=attempt,
                total=max_iters,
                label=f"Agentic retrieve {attempt}/{max_iters}",
            )
        )

    embedding = await embed_query(profile, query)
    chunks: list[dict[str, Any]] = []
    try:
        async for item in retrieve_mode(
            query, profile, embedding=embedding, mode=mode
        ):
            if isinstance(item, list):
                chunks = item
            else:
                _emit(item)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Retrieval failed in LangGraph node")
        # Do not SSE-emit here: empty chunks route to no_docs (single user error).
        return {
            "chunks": [],
            "attempt": attempt,
            "error": f"Retrieval failed: {exc}",
            "retrieval_failed": True,
        }

    return {
        "chunks": chunks,
        "attempt": attempt,
        "error": None,
        "retrieval_failed": False,
    }


async def node_grade(state: RetrievalState) -> dict[str, Any]:
    """Judge whether retrieved chunks answer the original question."""
    chunks = state.get("chunks") or []
    if not chunks:
        return {"relevant": False}
    try:
        relevant = await grade_relevant(state["question"], chunks, state["profile"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("grade_relevant failed (%s); treating as not relevant", exc)
        return {"relevant": False, "error": str(exc)}
    return {"relevant": bool(relevant), "error": None}


async def node_rewrite(state: RetrievalState) -> dict[str, Any]:
    """Rewrite the retrieval query for the next agentic iteration."""
    profile = state["profile"]
    query = state.get("query") or state["question"]
    try:
        rewritten = await rewrite_query(query, profile)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rewrite_query failed (%s); keeping prior query", exc)
        return {"error": str(exc)}
    return {"query": rewritten or query, "error": None}


async def node_expand(state: RetrievalState) -> dict[str, Any]:
    """Optionally expand chunk text to parent sections via vault reads."""
    chunks = list(state.get("chunks") or [])
    profile = state["profile"]
    if profile.expand_to_parent and chunks:
        chunks = expand_chunks(chunks)
    return {"chunks": chunks}


async def node_citations(state: RetrievalState) -> dict[str, Any]:
    """Emit CitationsEvent for chunks that carry a note path."""
    chunks = state.get("chunks") or []
    citations = [chunk_to_citation(c) for c in chunks if c.get("note_path")]
    if citations:
        _emit(CitationsEvent(citations=citations))
    return {}


async def node_no_docs(state: RetrievalState) -> dict[str, Any]:
    """Tell the client retrieval found nothing usable (one SSE error only)."""
    if state.get("retrieval_failed"):
        # Keep exception detail in logs only; user-facing text stays generic.
        _emit(
            ErrorEvent(
                message=(
                    "Retrieval failed. Check the index is ready and try again."
                ),
                code="retrieval_failed",
            )
        )
    else:
        _emit(
            ErrorEvent(
                message=(
                    "No relevant documents found. Try another query mode "
                    "or reindex the vault."
                )
            )
        )
    return {"generation_ok": False, "model_used": "no_docs"}


async def node_generate(state: RetrievalState) -> dict[str, Any]:
    """Stream answer tokens live; retry only if the attempt emitted nothing.

    Avoids appending a second answer after a mid-stream failure that already
    flushed tokens to the SSE client.
    """
    profile = state["profile"]
    chunks = state.get("chunks") or []
    attempt = int(state.get("generate_attempt") or 0) + 1
    context = "\n\n".join(
        f"[{c.get('note_title') or c.get('note_path') or 'context'}]\n{c.get('text', '')}"
        for c in chunks
    ) or "(no notes indexed yet)"
    policy = get_policy_engine()
    messages = build_rag_chat_messages(
        policy_text=policy.system_prompt(),
        retrieved_context=context,
        question=state["question"],
        history=list(state.get("history") or []),
    )
    model = build_chat_model(
        profile.chat_model,
        Provider(profile.chat_provider),
        max_context_tokens=profile.max_context_tokens,
    )
    assembled: list[str] = []
    try:
        async for chunk in model.astream(messages):
            text = getattr(chunk, "content", "") or ""
            if text:
                assembled.append(text)
                _emit(TokenEvent(text=text))
        label = "primary" if attempt == 1 else "retry"
        return {
            "generation_ok": True,
            "error": None,
            "model_used": label,
            "generate_attempt": attempt,
            "assembled": "".join(assembled),
            "prompt_messages": messages,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Generation attempt %s failed: %s", attempt, exc)
        # Mid-stream failure already flushed tokens — skip further retries.
        force_done = _MAX_GENERATE_ATTEMPTS if assembled else attempt
        return {
            "generation_ok": False,
            "error": "generation_failed",
            "generate_attempt": force_done,
            "model_used": "",
            "prompt_messages": messages,
            "assembled": "".join(assembled),
        }


async def node_error(state: RetrievalState) -> dict[str, Any]:
    """Graceful apology after primary + retry generation both fail."""
    _emit(
        ErrorEvent(
            message="Generation failed. Please try again in a moment.",
            code="generation_failed",
        )
    )
    apology = (
        "I'm sorry, I'm having trouble processing your request "
        "right now. Please try again in a moment."
    )
    _emit(TokenEvent(text=apology))
    return {
        "generation_ok": False,
        "model_used": "error_handler",
        "assembled": apology,
    }


def build_retrieval_graph():
    """Compile the hybrid + agentic + generate StateGraph."""
    graph = StateGraph(RetrievalState)

    graph.add_node("resolve_mode", node_resolve_mode)
    graph.add_node("retrieve", node_retrieve)
    graph.add_node("grade", node_grade)
    graph.add_node("rewrite", node_rewrite)
    graph.add_node("expand", node_expand)
    graph.add_node("citations", node_citations)
    graph.add_node("no_docs", node_no_docs)
    graph.add_node("generate", node_generate)
    graph.add_node("error", node_error)

    graph.add_edge(START, "resolve_mode")
    graph.add_edge("resolve_mode", "retrieve")
    graph.add_conditional_edges(
        "retrieve",
        route_after_retrieve,
        {"grade": "grade", "expand": "expand"},
    )
    graph.add_conditional_edges(
        "grade",
        route_after_grade,
        {"expand": "expand", "rewrite": "rewrite"},
    )
    graph.add_edge("rewrite", "retrieve")
    graph.add_edge("expand", "citations")
    graph.add_conditional_edges(
        "citations",
        route_after_citations,
        {"generate": "generate", "no_docs": "no_docs"},
    )
    graph.add_edge("no_docs", END)
    graph.add_conditional_edges(
        "generate",
        route_after_generate,
        {"done": END, "retry": "generate", "error": "error"},
    )
    graph.add_edge("error", END)

    return graph.compile()


_GRAPH = None


def get_retrieval_graph():
    """Return the process-wide compiled retrieval graph."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_retrieval_graph()
    return _GRAPH


def reset_retrieval_graph() -> None:
    """Drop the compiled graph (tests that need a fresh compile)."""
    global _GRAPH
    _GRAPH = None


async def stream_query(
    question: str,
    profile: Profile,
    history: list[ChatMessage] | None = None,
    *,
    metrics_out: dict[str, Any] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Run the retrieval graph and yield StreamEvents for SSE.

    Does not emit DoneEvent — the engine adds timing/metrics around this.
    When ``metrics_out`` is provided, it is filled with ``assembled`` and
    ``prompt_messages`` from the final graph state.
    """
    initial: RetrievalState = {
        "question": question,
        "query": question,
        "history": list(history or []),
        "profile": profile,
        "rag_mode": profile.rag_mode,
        "chunks": [],
        "attempt": 0,
        "max_iters": int(profile.agentic_max_iters),
        "generate_attempt": 0,
        "generation_ok": False,
        "error": None,
        "model_used": "",
        "assembled": "",
    }
    graph = get_retrieval_graph()
    final: RetrievalState | None = None
    async for mode, data in graph.astream(
        initial, stream_mode=["custom", "values"]
    ):
        if mode == "custom":
            event = _coerce_stream_event(data)
            if event is not None:
                yield event
        elif mode == "values" and isinstance(data, dict):
            final = data  # type: ignore[assignment]

    if metrics_out is not None and final is not None:
        metrics_out["assembled"] = final.get("assembled") or ""
        metrics_out["prompt_messages"] = final.get("prompt_messages") or []
        metrics_out["model_used"] = final.get("model_used") or ""


def _coerce_stream_event(item: Any) -> StreamEvent | None:
    """Accept StreamEvent models or dict payloads from the custom stream."""
    if isinstance(
        item,
        (
            TokenEvent,
            RetrievalStartEvent,
            RetrievalProgressEvent,
            CitationsEvent,
            ErrorEvent,
        ),
    ):
        return item  # type: ignore[return-value]
    if isinstance(item, dict) and item.get("type"):
        try:
            return StreamEventAdapter.validate_python(item)
        except Exception:  # noqa: BLE001
            return None
    return None


def prompt_token_count(messages: list[dict]) -> int:
    """Estimate tokens across chat message content fields."""
    return sum(estimate_tokens(str(m.get("content") or "")) for m in messages)
