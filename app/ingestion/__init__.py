"""Vault note chunking for ingestion."""

from __future__ import annotations

from app.ingestion.chunkers import EvidenceChunk, apply_chunker
from app.ingestion.effort import ChunkPlan, resolve_chunk_plan
from app.models import Profile

__all__ = [
    "ChunkPlan",
    "EvidenceChunk",
    "chunk_note",
    "resolve_chunk_plan",
]


def chunk_note(
    text: str,
    profile: Profile,
    *,
    title: str | None = None,
    document_id: str | None = None,
    embeddings=None,
    tags: list[str] | None = None,
) -> tuple[ChunkPlan, list[EvidenceChunk]]:
    """Resolve effort to a chunker and split ``text`` into evidence chunks.

    ``document_id`` and ``tags`` are attached as chunk metadata.
    ``embeddings`` is forwarded to semantic chunking when provided.
    """
    plan = resolve_chunk_plan(profile)
    chunks = apply_chunker(
        plan.chunker,
        text,
        profile,
        title=title,
        embeddings=embeddings,
        document_id=document_id,
        tags=tags,
    )
    return plan, chunks
