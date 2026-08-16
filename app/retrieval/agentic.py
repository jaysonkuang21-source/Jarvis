"""Agentic grade/rewrite — loop lives in LangGraph (`graph.py`).

Routing helpers are re-exported for unit tests. Prefer
``stream_query`` / ``PostgresHybridEngine.query`` for production chat.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.models import Profile, QueryMode, RetrievalProgressEvent, StreamEvent
from app.retrieval.graph import route_after_grade, route_after_retrieve
from app.retrieval.modes import retrieve
from app.retrieval.rerank import grade_relevant, rewrite_query

__all__ = [
    "agentic_retrieve",
    "route_after_grade",
    "route_after_retrieve",
]


async def agentic_retrieve(
    question: str,
    profile: Profile,
    *,
    embedding_fn,
    mode: QueryMode,
) -> AsyncIterator[StreamEvent | list[dict[str, Any]]]:
    """Back-compat agentic loop (same semantics as the LangGraph grade path).

    New chat traffic should use ``app.retrieval.graph.stream_query``. This
    helper remains for focused tests and callers that only need chunks.
    """
    query = question
    last: list[dict[str, Any]] = []
    max_iters = profile.agentic_max_iters

    for attempt in range(1, max_iters + 1):
        yield RetrievalProgressEvent(
            current=attempt,
            total=max_iters,
            label=f"Agentic retrieve {attempt}/{max_iters}",
        )
        embedding = await embedding_fn(query) if embedding_fn else None
        chunks: list[dict[str, Any]] = []
        async for item in retrieve(
            query, profile, embedding=embedding, mode=mode
        ):
            if isinstance(item, list):
                chunks = item
            else:
                yield item

        if not chunks:
            yield []
            return

        last = chunks
        try:
            relevant = await grade_relevant(question, chunks, profile)
        except Exception:  # noqa: BLE001
            relevant = False

        if relevant:
            yield chunks
            return

        if attempt < max_iters:
            try:
                query = await rewrite_query(query, profile)
            except Exception:  # noqa: BLE001
                pass

    yield last
