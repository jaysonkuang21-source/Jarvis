"""Ingestion effort and chunker tests (no Postgres)."""

from __future__ import annotations

from app.ingestion import chunk_note, resolve_chunk_plan
from app.ingestion.chunkers import extract_wikilinks
from app.models import Chunker, IngestEffort, Profile

SAMPLE = """# Project Alpha

Alpha links to [[Project Beta]] and keeps the claim in one place.

## Details

More context about Alpha. This paragraph is long enough that a small
chunk size will force a split somewhere near the wikilink [[Shared Note]]
if we are not careful about boundaries.

### Nested

Leaf section with no links.
"""


def test_manual_effort_uses_profile_chunker() -> None:
    profile = Profile(ingest_effort=IngestEffort.MANUAL, chunker=Chunker.RECURSIVE)
    plan = resolve_chunk_plan(profile)
    assert plan.chunker is Chunker.RECURSIVE
    assert not plan.needs_decision_model


def test_low_effort_forces_structure_entity() -> None:
    profile = Profile(
        ingest_effort=IngestEffort.LOW,
        chunker=Chunker.SEMANTIC,
    )
    plan = resolve_chunk_plan(profile)
    assert plan.chunker is Chunker.STRUCTURE_ENTITY


def test_medium_and_high_flag_decision_model() -> None:
    for effort in (IngestEffort.MEDIUM, IngestEffort.HIGH):
        plan = resolve_chunk_plan(Profile(ingest_effort=effort))
        assert plan.needs_decision_model
        assert plan.chunker is Chunker.STRUCTURE_ENTITY


def test_chunk_note_produces_offsets_inside_document() -> None:
    profile = Profile(
        ingest_effort=IngestEffort.MANUAL,
        chunker=Chunker.RECURSIVE,
        chunk_size=128,
        chunk_overlap=20,
        prepend_note_context=True,
    )
    plan, chunks = chunk_note(SAMPLE, profile, title="Alpha", document_id="Alpha.md")
    assert plan.chunker is Chunker.RECURSIVE
    assert chunks
    for chunk in chunks:
        assert 0 <= chunk.char_start < chunk.char_end <= len(SAMPLE)
        assert chunk.chunk_id
        assert SAMPLE[chunk.char_start : chunk.char_end]
        assert chunk.document_id == "Alpha.md"
        assert chunk.doc_title == "Alpha"
        assert chunk.page is None


def test_structure_entity_attaches_section_metadata() -> None:
    profile = Profile(
        ingest_effort=IngestEffort.MANUAL,
        chunker=Chunker.STRUCTURE_ENTITY,
        chunk_size=128,
        chunk_overlap=20,
        prepend_note_context=False,
    )
    _, chunks = chunk_note(
        SAMPLE,
        profile,
        title="Alpha",
        document_id="notes/Alpha.md",
        tags=["project", "alpha"],
    )
    assert chunks
    for chunk in chunks:
        assert chunk.document_id == "notes/Alpha.md"
        assert chunk.doc_title == "Alpha"
        assert chunk.tags == ["project", "alpha"]
        if chunk.heading_path:
            assert chunk.section == chunk.heading_path[-1]


def test_structure_entity_keeps_wikilink_intact() -> None:
    # Bypass Profile ge=128 so a tiny window would otherwise bisect a link.
    profile = Profile.model_construct(
        ingest_effort=IngestEffort.LOW,
        chunker=Chunker.STRUCTURE_ENTITY,
        chunk_size=8,
        chunk_overlap=1,
        prepend_note_context=False,
    )
    _, chunks = chunk_note(SAMPLE, profile, title="Alpha")
    joined = " ".join(c.text for c in chunks)
    assert "[[Project Beta]]" in joined
    assert "[[Shared Note]]" in joined
    for chunk in chunks:
        for link in chunk.wikilinks:
            assert link in chunk.text
            assert "[[" in link and "]]" in link
        # No chunk text should contain a truncated wikilink opener without closer.
        assert chunk.text.count("[[") == chunk.text.count("]]")


def test_claim_centered_uses_sentence_spans() -> None:
    profile = Profile(
        ingest_effort=IngestEffort.MANUAL,
        chunker=Chunker.CLAIM_CENTERED,
        chunk_size=128,
        chunk_overlap=10,
        prepend_note_context=False,
    )
    plan, chunks = chunk_note(
        SAMPLE, profile, title="Alpha", document_id="Alpha.md", tags=["t"]
    )
    assert plan.chunker is Chunker.CLAIM_CENTERED
    assert chunks
    for chunk in chunks:
        assert SAMPLE[chunk.char_start : chunk.char_end]
        assert chunk.text.count("[[") == chunk.text.count("]]")
        assert chunk.document_id == "Alpha.md"
        assert chunk.tags == ["t"]


def test_semantic_without_embeddings_falls_back_to_recursive() -> None:
    profile = Profile(
        ingest_effort=IngestEffort.MANUAL,
        chunker=Chunker.SEMANTIC,
        chunk_size=128,
        chunk_overlap=20,
    )
    plan, chunks = chunk_note(SAMPLE, profile, title="Alpha")
    assert plan.chunker is Chunker.SEMANTIC
    assert chunks


def test_extract_wikilinks_unique_order() -> None:
    text = "See [[A]] then [[B]] and [[A]] again."
    assert extract_wikilinks(text) == ["[[A]]", "[[B]]"]
