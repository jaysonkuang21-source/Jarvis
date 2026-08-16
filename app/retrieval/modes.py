"""Query-mode strategies: Local, Global, DRIFT, Auto."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.agent import build_chat_model
from app.config import get_settings
from app.db import repo
from app.ingestion.formats import heuristic_retriever_tags
from app.ingestion.tags import extract_query_tags
from app.models import (
    Profile,
    Provider,
    QueryMode,
    RetrievalProgressEvent,
    StreamEvent,
)
from app.retrieval.hybrid import MetadataFilter, hybrid_search
from app.retrieval.rerank import rerank_chunks
from app.retrieval.result_cache import cache_key, get_retrieval_cache


def _aux_timeout_seconds() -> float:
    """Timeout budget for helper LLM calls in retrieval routing."""
    return max(1.0, float(get_settings().llm_aux_timeout_seconds))


async def resolve_mode(question: str, profile: Profile) -> QueryMode:
    """Auto mode: ask a fast LLM which query mode to use."""
    if profile.query_mode is not QueryMode.AUTO:
        return profile.query_mode

    model = build_chat_model(
        profile.chunk_decision_model, Provider(profile.chunk_decision_provider)
    )
    prompt = (
        "Choose the best retrieval mode for this question about a personal vault.\n"
        "Reply with exactly one word: local, global, or drift.\n"
        "- local: specific facts, people, notes, entities\n"
        "- global: themes, overviews, synthesis across many notes\n"
        "- drift: start broad then drill into promising areas\n\n"
        f"Question: {question}"
    )
    try:
        response = await asyncio.wait_for(
            model.ainvoke(prompt),
            timeout=_aux_timeout_seconds(),
        )
        raw = (getattr(response, "content", "") or str(response)).strip().lower()
        for mode in (QueryMode.LOCAL, QueryMode.GLOBAL, QueryMode.DRIFT):
            if mode.value in raw:
                return mode
    except Exception:  # noqa: BLE001
        pass
    return QueryMode.LOCAL


async def retrieve(
    question: str,
    profile: Profile,
    *,
    embedding: list[float] | None,
    mode: QueryMode,
    filters: MetadataFilter | None = None,
) -> AsyncIterator[StreamEvent | list[dict[str, Any]]]:
    """Run the selected mode; yields progress events then a final chunk list.

    The last yielded value is always ``list[dict]`` of chunks.
    Pipeline for Local: metadata filter → result cache → hybrid ANN+FTS →
    graph expand → rerank.
    """
    if mode is QueryMode.GLOBAL:
        async for item in _global(question, profile):
            yield item
        return
    if mode is QueryMode.DRIFT:
        async for item in _drift(
            question, profile, embedding=embedding, filters=filters
        ):
            yield item
        return
    async for item in _local(
        question, profile, embedding=embedding, filters=filters, mode=mode
    ):
        yield item


def _local_db_retrieve(
    question: str,
    embedding: list[float] | None,
    profile: Profile,
    filt: MetadataFilter,
) -> list[dict[str, Any]]:
    """Blocking hybrid + graph-neighborhood expansion for local mode."""
    fused = hybrid_search(question, embedding, profile, filters=filt)
    seed_ids = repo.entity_ids_for_chunks([int(c["id"]) for c in fused])
    neighbors = repo.neighbor_entity_ids(seed_ids, hops=2)
    graph_chunks = repo.chunks_for_entities(neighbors, limit=profile.top_k * 2)
    return _merge_by_id(fused + graph_chunks)


async def _resolve_tag_filter(
    question: str,
    profile: Profile,
    filters: MetadataFilter | None,
) -> tuple[MetadataFilter, str]:
    """Fill MetadataFilter.tags; prefer file-type retriever tags when cued.

    Explicit ``MetadataFilter()`` (e.g. after an empty-tag fallback) is left as-is
    so we do not re-extract and re-filter the same miss.
    Returns ``(filter, progress_label)``.
    """
    if filters is not None:
        return filters, "Metadata filter"

    modality = heuristic_retriever_tags(question)
    if modality:
        # Modality tags alone select the retriever (AND with topical tags would miss).
        if any("visual" in t for t in modality):
            label = "Retriever: visual"
        elif any("kind-pdf" in t for t in modality):
            label = "Retriever: pdf (text-hybrid)"
        elif any("binary" in t for t in modality):
            label = "Retriever: binary-meta"
        else:
            label = "Retriever: text-hybrid"
        return MetadataFilter(tags=tuple(modality)), label

    tags = await extract_query_tags(question, profile)
    if not tags:
        return MetadataFilter(), "Retriever: text-hybrid"
    return MetadataFilter(tags=tuple(tags)), "Metadata filter"


async def _local(
    question: str,
    profile: Profile,
    *,
    embedding: list[float] | None,
    filters: MetadataFilter | None = None,
    mode: QueryMode = QueryMode.LOCAL,
) -> AsyncIterator[StreamEvent | list[dict[str, Any]]]:
    """Filter by tags/retriever, cache, hybrid retrieve, expand, rerank."""
    filt, filter_label = await _resolve_tag_filter(question, profile, filters)
    yield RetrievalProgressEvent(current=1, total=4, label=filter_label)

    cache = get_retrieval_cache()
    key = cache_key(question, profile, mode, filt)

    yield RetrievalProgressEvent(current=2, total=4, label="Checking cache")
    cached = cache.get(key)
    if cached is not None:
        yield cached
        return

    yield RetrievalProgressEvent(current=3, total=4, label="Hybrid ANN search")
    merged = await asyncio.to_thread(
        _local_db_retrieve, question, embedding, profile, filt
    )

    # Never empty from tags alone — retry without tag constraints.
    if not merged and filt.tags:
        yield RetrievalProgressEvent(
            current=3, total=4, label="Tag filter miss — full search"
        )
        filt = MetadataFilter(path_prefix=filt.path_prefix, tags=())
        key = cache_key(question, profile, mode, filt)
        cached = cache.get(key)
        if cached is not None:
            yield cached
            return
        merged = await asyncio.to_thread(
            _local_db_retrieve, question, embedding, profile, filt
        )

    yield RetrievalProgressEvent(current=4, total=4, label="Reranking")
    ranked = await rerank_chunks(question, merged[: profile.top_k * 2], profile)
    result = ranked[: profile.top_k]
    cache.set(key, result)
    yield result


async def _global(
    question: str, profile: Profile
) -> AsyncIterator[StreamEvent | list[dict[str, Any]]]:
    """Map-reduce over community summaries."""
    reports = await asyncio.to_thread(
        repo.list_community_reports, profile.community_level
    )
    if not reports:
        yield []
        return

    model = build_chat_model(profile.chat_model, Provider(profile.chat_provider))
    total = len(reports)
    partials: list[str] = []
    for i, report in enumerate(reports, start=1):
        yield RetrievalProgressEvent(
            current=i, total=total + 1, label=f"Community {report.get('label') or i}"
        )
        prompt = (
            "Using only this community summary, note anything relevant to the "
            "question. If nothing is relevant, reply NONE.\n\n"
            f"Question: {question}\n\nSummary:\n{report['summary']}"
        )
        try:
            response = await model.ainvoke(prompt)
            text = (getattr(response, "content", "") or str(response)).strip()
            if text and text.upper() != "NONE":
                partials.append(text)
        except Exception:  # noqa: BLE001
            continue

    yield RetrievalProgressEvent(current=total + 1, total=total + 1, label="Reduce")
    # Represent map outputs as synthetic chunks for citation-less synthesis context.
    chunks = [
        {
            "id": f"community-{i}",
            "chunk_id": f"community-{i}",
            "text": text,
            "heading_path": [],
            "char_start": 0,
            "char_end": 0,
            "note_path": "",
            "note_title": "Community report",
            "score": 1.0,
            "source": "graph",
        }
        for i, text in enumerate(partials)
    ]
    yield chunks


async def _drift(
    question: str,
    profile: Profile,
    *,
    embedding: list[float] | None,
    filters: MetadataFilter | None = None,
) -> AsyncIterator[StreamEvent | list[dict[str, Any]]]:
    """Probe communities, then run constrained local retrieval."""
    yield RetrievalProgressEvent(current=1, total=3, label="Probing communities")
    filt, _label = await _resolve_tag_filter(question, profile, filters)
    reports = await asyncio.to_thread(
        repo.list_community_reports, profile.community_level, 12
    )
    # Use hybrid as a cheap probe when reports are sparse.
    probe = await asyncio.to_thread(
        lambda: hybrid_search(question, embedding, profile, filters=filt)
    )
    if not probe and filt.tags:
        filt = MetadataFilter(path_prefix=filt.path_prefix, tags=())
        probe = await asyncio.to_thread(
            lambda: hybrid_search(question, embedding, profile, filters=filt)
        )
    yield RetrievalProgressEvent(current=2, total=3, label="Drilling into regions")
    local_chunks: list[dict[str, Any]] = []
    async for item in _local(
        question, profile, embedding=embedding, filters=filt, mode=QueryMode.DRIFT
    ):
        if isinstance(item, list):
            local_chunks = item
    # Prefer local evidence; keep short community snippets if useful.
    extras = [
        {
            "id": f"drift-c-{r['id']}",
            "chunk_id": f"drift-c-{r['id']}",
            "text": r["summary"][:800],
            "heading_path": [],
            "char_start": 0,
            "char_end": 0,
            "note_path": "",
            "note_title": r.get("label") or "Community",
            "score": 0.4,
            "source": "graph",
        }
        for r in reports[:3]
    ]
    merged = _merge_by_id(local_chunks + probe + extras)
    yield RetrievalProgressEvent(current=3, total=3, label="Reranking")
    ranked = await rerank_chunks(question, merged, profile)
    yield ranked[: profile.top_k]


def _merge_by_id(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate chunks by id, keeping the higher score."""
    best: dict[Any, dict[str, Any]] = {}
    for chunk in chunks:
        key = chunk.get("id")
        prev = best.get(key)
        if prev is None or float(chunk.get("score") or 0) > float(prev.get("score") or 0):
            best[key] = chunk
    return sorted(best.values(), key=lambda c: float(c.get("score") or 0), reverse=True)
