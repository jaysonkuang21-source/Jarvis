"""Chunker dispatch and offset integrity tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.ingestion.chunkers import apply_chunker, estimate_tokens
from app.models import Chunker, Profile

SAMPLE = """# Project Alpha

Alpha links to [[Project Beta]] and keeps the claim in one place.

## Details

More context about Alpha. This paragraph is long enough that a small
chunk size will force a split somewhere near the wikilink [[Shared Note]]
if we are not careful about boundaries.

### Nested

Leaf section with no links.
"""


class _StubEmbeddings:
    """Offline embeddings stand-in for SemanticChunker."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Return deterministic unit vectors keyed on text length."""
        return [[float(len(t) % 7), 1.0, 0.0] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """Mirror embed_documents for a single query string."""
        return self.embed_documents([text])[0]


def test_estimate_tokens_empty() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_positive() -> None:
    assert estimate_tokens("hello world") > 0


def test_apply_chunker_recursive_offsets(profile: Profile) -> None:
    profile = profile.model_copy(
        update={
            "chunker": Chunker.RECURSIVE,
            "chunk_size": 128,
            "chunk_overlap": 20,
            "prepend_note_context": False,
        }
    )
    chunks = apply_chunker(Chunker.RECURSIVE, SAMPLE, profile, title="Alpha")
    assert chunks
    for chunk in chunks:
        assert SAMPLE[chunk.char_start : chunk.char_end]
        assert 0 <= chunk.char_start < chunk.char_end <= len(SAMPLE)


def test_apply_chunker_structure_offsets() -> None:
    profile = Profile.model_construct(
        chunker=Chunker.STRUCTURE_ENTITY,
        chunk_size=128,
        chunk_overlap=20,
        prepend_note_context=False,
    )
    chunks = apply_chunker(Chunker.STRUCTURE_ENTITY, SAMPLE, profile, title="Alpha")
    assert chunks
    for chunk in chunks:
        assert SAMPLE[chunk.char_start : chunk.char_end]


def test_apply_chunker_claim_offsets(profile: Profile) -> None:
    profile = profile.model_copy(
        update={
            "chunker": Chunker.CLAIM_CENTERED,
            "chunk_size": 128,
            "chunk_overlap": 10,
            "prepend_note_context": False,
        }
    )
    chunks = apply_chunker(Chunker.CLAIM_CENTERED, SAMPLE, profile, title="Alpha")
    assert chunks
    for chunk in chunks:
        assert SAMPLE[chunk.char_start : chunk.char_end]


def test_apply_chunker_semantic_without_embeddings_falls_back(profile: Profile) -> None:
    profile = profile.model_copy(
        update={"chunk_size": 128, "chunk_overlap": 20, "prepend_note_context": False}
    )
    chunks = apply_chunker(Chunker.SEMANTIC, SAMPLE, profile, title="Alpha")
    assert chunks


def test_apply_chunker_semantic_with_stub_embeddings(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LangChain SemanticChunker path is exercised offline via a stub class."""
    from langchain_core.documents import Document

    profile = profile.model_copy(
        update={"chunk_size": 128, "chunk_overlap": 20, "prepend_note_context": False}
    )

    class FakeSemanticChunker:
        """Stand-in that mimics LangChain SemanticChunker.create_documents."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Accept embeddings / threshold kwargs and ignore them."""

        def create_documents(self, texts: list[str]) -> list[Document]:
            """Split on blank lines and attach start_index like LangChain."""
            text = texts[0]
            parts = [p for p in text.split("\n\n") if p.strip()]
            docs: list[Document] = []
            search_from = 0
            for part in parts:
                pos = text.find(part, search_from)
                if pos < 0:
                    pos = text.find(part)
                if pos < 0:
                    continue
                docs.append(
                    Document(page_content=part, metadata={"start_index": pos})
                )
                search_from = pos + 1
            return docs

    monkeypatch.setattr(
        "langchain_experimental.text_splitter.SemanticChunker",
        FakeSemanticChunker,
    )
    embeddings: Any = _StubEmbeddings()
    chunks = apply_chunker(
        Chunker.SEMANTIC,
        SAMPLE,
        profile,
        title="Alpha",
        embeddings=embeddings,
    )
    assert chunks
    for chunk in chunks:
        assert 0 <= chunk.char_start < chunk.char_end <= len(SAMPLE)
        assert SAMPLE[chunk.char_start : chunk.char_end]
