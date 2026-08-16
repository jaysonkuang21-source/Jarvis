"""Chunker quality checks using short fixture notes.

Measures keyword retention, topic separation, and multi-format survival across
recursive, structure_entity, and claim_centered chunkers. Semantic is covered
separately because stub embeddings are not a real model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingestion.chunkers import apply_chunker
from app.models import Chunker, Profile

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "chunkers"

# Offline-stable chunkers (no embedding model required).
_OFFLINE = (
    Chunker.RECURSIVE,
    Chunker.STRUCTURE_ENTITY,
    Chunker.CLAIM_CENTERED,
)


class _StubEmbeddings:
    """Offline embeddings: separate vectors when section cues differ."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Return crude topic vectors from keyword presence."""
        out: list[list[float]] = []
        for text in texts:
            lower = text.lower()
            habitat = 1.0 if "habitat" in lower or "airlock" in lower else 0.0
            rag = 1.0 if "retrieval" in lower or "chunk" in lower else 0.0
            out.append([habitat, rag, float(len(text) % 5)])
        return out

    def embed_query(self, text: str) -> list[float]:
        """Mirror embed_documents for a single string."""
        return self.embed_documents([text])[0]


def _load(name: str) -> str:
    """Read a chunker fixture markdown file."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def _profile(chunker: Chunker, size: int = 160, overlap: int = 20) -> Profile:
    """Build a profile with context prepending disabled for offset tests."""
    return Profile.model_construct(
        chunker=chunker,
        chunk_size=size,
        chunk_overlap=overlap,
        prepend_note_context=False,
    )


def _joined(chunks: list) -> str:
    """Concatenate chunk texts for membership checks."""
    return "\n".join(c.text for c in chunks)


@pytest.mark.parametrize("chunker", list(_OFFLINE))
def test_keyword_tokens_survive_all_chunkers(chunker: Chunker) -> None:
    """Rare keywords must appear in at least one emitted chunk (no keyword loss)."""
    text = _load("keyword_dense.md")
    profile = _profile(chunker, size=120, overlap=24)
    chunks = apply_chunker(chunker, text, profile, title="Keyword")
    assert chunks
    joined = _joined(chunks)
    assert "ZYPHERON-9" in joined
    assert "KWLOSS-ALPHA-77" in joined
    for chunk in chunks:
        assert 0 <= chunk.char_start < chunk.char_end <= len(text)


def test_structure_chunker_reduces_topic_mixing() -> None:
    """Heading-aware chunking should keep habitat and RAG cues separable."""
    text = _load("semantic_shift.md")
    profile = _profile(Chunker.STRUCTURE_ENTITY, size=180, overlap=0)
    chunks = apply_chunker(
        Chunker.STRUCTURE_ENTITY, text, profile, title="Semantic"
    )
    assert len(chunks) >= 2
    joined = _joined(chunks)
    assert "airlock" in joined.lower()
    assert "retrieval" in joined.lower() or "knowledge assistants" in joined.lower()
    mixed = [
        c
        for c in chunks
        if "airlock" in c.text.lower()
        and ("retrieval" in c.text.lower() or "rag" in c.text.lower())
    ]
    assert len(mixed) < len(chunks)

@pytest.mark.parametrize("chunker", list(_OFFLINE))
def test_mixed_structure_fixture_chunkers(chunker: Chunker) -> None:
    """Lists, wikilinks, and fenced code must not crash offline chunkers."""
    text = _load("mixed_structure.md")
    profile = _profile(chunker, size=140, overlap=20)
    chunks = apply_chunker(chunker, text, profile, title="Mixed")
    assert chunks
    joined = _joined(chunks)
    assert "COLLOID-BRIDGE" in joined
    assert "Shared Protocol" in joined or "[[Shared Protocol]]" in joined


def test_claim_centered_keeps_claim_sentences() -> None:
    """Claim-centered path should retain each labeled claim string."""
    text = _load("claim_units.md")
    profile = _profile(Chunker.CLAIM_CENTERED, size=80, overlap=0)
    chunks = apply_chunker(
        Chunker.CLAIM_CENTERED, text, profile, title="Claims"
    )
    joined = _joined(chunks)
    assert "Claim A:" in joined
    assert "Claim B:" in joined
    assert "Claim C:" in joined
    assert "QUARTZ-DELTA" in joined


def test_semantic_chunker_runs_with_stub_embeddings() -> None:
    """Semantic path must accept embeddings without raising (may yield few chunks)."""
    text = _load("semantic_shift.md")
    profile = _profile(Chunker.SEMANTIC, size=120, overlap=10)
    chunks = apply_chunker(
        Chunker.SEMANTIC,
        text,
        profile,
        title="Semantic",
        embeddings=_StubEmbeddings(),
    )
    # Stub vectors are weak; either real pieces or empty is acceptable —
    # the contract under test is "does not crash".
    assert isinstance(chunks, list)
