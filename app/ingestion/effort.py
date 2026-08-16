"""Map ingestion effort to an effective chunker plan."""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Chunker, IngestEffort, Profile


@dataclass(frozen=True)
class ChunkPlan:
    """Resolved strategy for one ingest pass (no LLM calls in phase C)."""

    chunker: Chunker
    effort: IngestEffort
    needs_decision_model: bool = False
    notes: str = ""


def resolve_chunk_plan(profile: Profile) -> ChunkPlan:
    """Choose the chunker implied by ``profile.ingest_effort``.

    Medium and high still return a deterministic placeholder strategy until the
    decision / evaluator LLM path is implemented. Callers can see
    ``needs_decision_model`` and defer or fall back.
    """
    effort = profile.ingest_effort

    if effort is IngestEffort.MANUAL:
        return ChunkPlan(
            chunker=profile.chunker,
            effort=effort,
            notes="User-selected chunker.",
        )

    if effort is IngestEffort.LOW:
        return ChunkPlan(
            chunker=Chunker.STRUCTURE_ENTITY,
            effort=effort,
            notes="Low effort always uses structure + wikilink-aware chunking.",
        )

    if effort is IngestEffort.MEDIUM:
        return ChunkPlan(
            chunker=Chunker.STRUCTURE_ENTITY,
            effort=effort,
            needs_decision_model=True,
            notes=(
                "Medium effort will ask the decision model to pick among "
                "structure_entity, claim_centered, semantic, and recursive; "
                "phase C falls back to structure_entity."
            ),
        )

    # HIGH
    return ChunkPlan(
        chunker=Chunker.STRUCTURE_ENTITY,
        effort=effort,
        needs_decision_model=True,
        notes=(
            "High effort will run multiple chunkers and score them; "
            "phase C falls back to structure_entity."
        ),
    )
