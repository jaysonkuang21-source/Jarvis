"""Open tag normalize/merge and query-tag prefilter tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.ingestion.effort import ChunkPlan
from app.ingestion.tags import (
    extract_query_tags,
    merge_tags,
    normalize_tag,
    parse_tags_json,
    suggest_document_tags,
)
from app.models import Chunker, IngestEffort, Profile, QueryMode, RetrievalProgressEvent
from app.retrieval.hybrid import MetadataFilter
from app.retrieval.result_cache import cache_key


def test_normalize_tag_slugs_and_rejects_junk() -> None:
    """Freeform tags become lowercase kebab-case; junk is dropped."""
    assert normalize_tag("  #Project Alpha ") == "project-alpha"
    assert normalize_tag("hello_world") == "hello-world"
    assert normalize_tag("!!!") is None
    assert normalize_tag("") is None


def test_normalize_and_merge_tags_preserve_order_and_cap() -> None:
    """Earlier lists win; duplicates collapse; doc cap applies."""
    merged = merge_tags(
        ["Alpha", "beta"],
        ["beta", "Gamma", "extra-1", "extra-2", "extra-3", "extra-4", "extra-5", "extra-6"],
        limit=8,
    )
    assert merged[0] == "alpha"
    assert merged[1] == "beta"
    assert merged[2] == "gamma"
    assert len(merged) == 8


def test_parse_tags_json_from_llm_blob() -> None:
    """JSON tags survive surrounding prose from a small model."""
    raw = 'Sure.\n{"tags":["Travel", " #food "]}\n'
    assert parse_tags_json(raw, limit=3) == ["travel", "food"]


@pytest.mark.asyncio
async def test_suggest_document_tags_merges_via_model(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Document tag suggestions come from the rerank/evaluator model."""

    class FakeModel:
        async def ainvoke(self, _prompt: str) -> Any:
            class Resp:
                content = '{"tags":["Recipes", "Kitchen"]}'

            return Resp()

    monkeypatch.setattr(
        "app.ingestion.tags.build_chat_model", lambda *a, **k: FakeModel()
    )
    tags = await suggest_document_tags(profile, title="Soup", body="Broth notes")
    assert tags == ["recipes", "kitchen"]


@pytest.mark.asyncio
async def test_extract_query_tags(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    """Query tags are capped and normalized."""

    class FakeModel:
        async def ainvoke(self, _prompt: str) -> Any:
            class Resp:
                content = '{"tags":["Recipes", "dinner", "leftover", "noise"]}'

            return Resp()

    monkeypatch.setattr(
        "app.ingestion.tags.build_chat_model", lambda *a, **k: FakeModel()
    )
    tags = await extract_query_tags("what recipes for dinner?", profile)
    assert tags == ["recipes", "dinner", "leftover"]


@pytest.mark.asyncio
async def test_local_applies_query_tags_then_falls_back(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty tag-filtered hybrid retries unfiltered before rerank."""
    from app.retrieval import modes as modes_mod

    async def fake_extract(question: str, profile: Profile) -> list[str]:
        assert "soup" in question.lower()
        return ["recipes"]

    calls: list[MetadataFilter] = []

    def fake_db(
        question: str,
        embedding: list[float] | None,
        profile: Profile,
        filt: MetadataFilter,
    ) -> list[dict[str, Any]]:
        calls.append(filt)
        if filt.tags:
            return []
        return [
            {
                "id": 1,
                "chunk_id": "c0000",
                "text": "soup",
                "score": 0.9,
                "note_path": "a.md",
                "note_title": "A",
            }
        ]

    async def fake_rerank(
        question: str, chunks: list[dict[str, Any]], profile: Profile
    ) -> list[dict[str, Any]]:
        return chunks

    monkeypatch.setattr(modes_mod, "extract_query_tags", fake_extract)
    monkeypatch.setattr(modes_mod, "_local_db_retrieve", fake_db)
    monkeypatch.setattr(modes_mod, "rerank_chunks", fake_rerank)
    monkeypatch.setattr(
        modes_mod, "get_retrieval_cache", lambda: _NullCache()
    )

    events: list[Any] = []
    async for item in modes_mod._local(
        "soup recipes?", profile, embedding=[0.1], filters=None
    ):
        events.append(item)

    assert any(
        isinstance(e, RetrievalProgressEvent)
        and "Tag filter miss" in (e.label or "")
        for e in events
    )
    assert len(calls) == 2
    assert calls[0].tags == ("recipes",)
    assert calls[1].tags == ()
    assert isinstance(events[-1], list)
    assert events[-1][0]["id"] == 1


@pytest.mark.asyncio
async def test_local_skips_extract_when_filters_provided(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit filters (including empty) must not re-call the tag LLM."""
    from app.retrieval import modes as modes_mod

    extract = AsyncMock(return_value=["should-not-run"])
    monkeypatch.setattr(modes_mod, "extract_query_tags", extract)

    def fake_db(
        question: str,
        embedding: list[float] | None,
        profile: Profile,
        filt: MetadataFilter,
    ) -> list[dict[str, Any]]:
        return [{"id": 2, "text": "x", "score": 1.0}]

    async def fake_rerank(
        question: str, chunks: list[dict[str, Any]], profile: Profile
    ) -> list[dict[str, Any]]:
        return chunks

    monkeypatch.setattr(modes_mod, "_local_db_retrieve", fake_db)
    monkeypatch.setattr(modes_mod, "rerank_chunks", fake_rerank)
    monkeypatch.setattr(modes_mod, "get_retrieval_cache", lambda: _NullCache())

    filt = MetadataFilter(tags=("kitchen",))
    async for _ in modes_mod._local(
        "anything", profile, embedding=None, filters=filt
    ):
        pass
    extract.assert_not_awaited()

    # Cache keys differ when tags differ.
    a = cache_key("q", profile, QueryMode.LOCAL, MetadataFilter(tags=("a",)))
    b = cache_key("q", profile, QueryMode.LOCAL, MetadataFilter(tags=("b",)))
    assert a != b


@pytest.mark.asyncio
async def test_imbue_document_tags_merges_frontmatter(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest imbuement keeps frontmatter tags ahead of LLM suggestions."""
    from app.ingestion.index import _imbue_document_tags
    from app.ingestion.prepare import PreparedNote

    async def fake_suggest(profile: Profile, *, title: str, body: str) -> list[str]:
        return ["llm-only", "shared"]

    monkeypatch.setattr("app.ingestion.index.suggest_document_tags", fake_suggest)
    prepared = PreparedNote(
        original="---\ntags: [yaml]\n---\nbody about shared topic",
        working="body about shared topic",
        spans=[],
        title="Note",
        tags=["shared", "yaml"],
        body_offset=22,
    )
    plan = ChunkPlan(chunker=Chunker.STRUCTURE_ENTITY, effort=IngestEffort.MANUAL)
    tags = await _imbue_document_tags(profile, "Note", prepared, plan=plan)
    assert tags[0] == "shared"
    assert tags[1] == "yaml"
    assert "llm-only" in tags


@pytest.mark.asyncio
async def test_imbue_document_tags_skips_llm_for_recursive(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recursive chunking keeps frontmatter tags only."""
    from app.ingestion.effort import ChunkPlan
    from app.ingestion.index import _imbue_document_tags
    from app.ingestion.prepare import PreparedNote
    from app.models import Chunker, IngestEffort

    called = False

    async def fake_suggest(*_a, **_k) -> list[str]:
        nonlocal called
        called = True
        return ["should-not-run"]

    monkeypatch.setattr("app.ingestion.index.suggest_document_tags", fake_suggest)
    prepared = PreparedNote(
        original="---\ntags: [yaml]\n---\nbody",
        working="body",
        spans=[],
        tags=["yaml"],
        body_offset=22,
    )
    plan = ChunkPlan(chunker=Chunker.RECURSIVE, effort=IngestEffort.MANUAL)
    tags = await _imbue_document_tags(profile, "Note", prepared, plan=plan)
    assert tags == ["yaml"]
    assert not called


@pytest.mark.asyncio
async def test_imbue_document_tags_passes_body_without_frontmatter(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tag LLM sees note body only, not YAML frontmatter."""
    from app.ingestion.index import _imbue_document_tags
    from app.ingestion.prepare import PreparedNote

    seen: list[str] = []

    async def fake_suggest(profile: Profile, *, title: str, body: str) -> list[str]:
        seen.append(body)
        return ["topic"]

    monkeypatch.setattr("app.ingestion.index.suggest_document_tags", fake_suggest)
    prepared = PreparedNote(
        original="---\ntags: [secret]\n---\nReal body text here.",
        working="Real body text here.",
        spans=[],
        tags=["secret"],
        body_offset=22,
    )
    plan = ChunkPlan(chunker=Chunker.STRUCTURE_ENTITY, effort=IngestEffort.MANUAL)
    await _imbue_document_tags(profile, "Note", prepared, plan=plan)
    assert seen == ["Real body text here."]


@pytest.mark.asyncio
async def test_index_note_refreshes_tags_when_content_unchanged(
    profile: Profile, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reindex must still imbue tags when the note hash is already indexed."""
    from app.ingestion import index as index_mod

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# Hello\n\nSome astronomy notes.\n", encoding="utf-8")
    digest = __import__("hashlib").sha256(note.read_text(encoding="utf-8").encode()).hexdigest()

    upserts: list[list[str]] = []

    monkeypatch.setattr(
        index_mod.repo,
        "document_by_hash",
        lambda rel: (1, digest) if rel == "note.md" else None,
    )
    monkeypatch.setattr(
        index_mod.repo,
        "upsert_document",
        lambda rel, title, mtime, content_hash, tags=None: upserts.append(list(tags or [])) or 1,
    )
    monkeypatch.setattr(index_mod.repo, "delete_document_chunks", lambda _id: None)
    monkeypatch.setattr(
        index_mod,
        "_imbue_document_tags",
        AsyncMock(return_value=["astronomy", "notes"]),
    )
    monkeypatch.setattr(
        index_mod,
        "resolve_chunk_plan",
        lambda _p: ChunkPlan(
            chunker=Chunker.STRUCTURE_ENTITY,
            effort=IngestEffort.MANUAL,
        ),
    )

    profile = profile.model_copy(
        update={"ingest_effort": "manual", "chunker": "structure_entity"}
    )
    await index_mod._index_note(vault, note, profile)
    assert upserts == [["astronomy", "notes"]]


class _NullCache:
    """In-memory no-op cache for retrieval mode tests."""

    def get(self, key: str) -> None:
        """Always miss."""
        return None

    def set(self, key: str, value: list[dict[str, Any]]) -> None:
        """Ignore writes."""
        return None
