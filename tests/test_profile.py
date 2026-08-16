"""Profile validation tests.

The UI derives its disabled states from ``validate_profile``, so these cases
are the contract for which combinations are impossible versus merely slow.
"""

from __future__ import annotations

from app.models import (
    Chunker,
    IngestEffort,
    IngestMode,
    IssueLevel,
    ModelInfo,
    Profile,
    Provider,
    QueryMode,
    RagMode,
    validate_profile,
)

TEXT_MODEL = ModelInfo(
    id="qwen3.5:9b", provider=Provider.OLLAMA, label="qwen3.5:9b", supports_vision=True
)
VISION_MODEL = ModelInfo(
    id="qwen2.5vl:7b", provider=Provider.OLLAMA, label="qwen2.5vl:7b", supports_vision=True
)
FAST_MODEL = ModelInfo(
    id="qwen3.5:2b", provider=Provider.OLLAMA, label="qwen3.5:2b", supports_vision=True
)
LEGACY_FAST = ModelInfo(
    id="qwen2.5:3b", provider=Provider.OLLAMA, label="qwen2.5:3b", supports_vision=False
)
LEGACY_CHAT = ModelInfo(
    id="qwen3:8b", provider=Provider.OLLAMA, label="qwen3:8b", supports_vision=False
)
EMBED_MODEL = ModelInfo(
    id="qwen3-embedding:8b",
    provider=Provider.OLLAMA,
    label="qwen3-embedding:8b",
    is_embedding=True,
    dimensions=4096,
)
MODELS = {
    m.id: m
    for m in (TEXT_MODEL, VISION_MODEL, FAST_MODEL, LEGACY_FAST, LEGACY_CHAT, EMBED_MODEL)
}


def errors(result) -> list[str]:
    return [i.field for i in result.issues if i.level is IssueLevel.ERROR]


def test_defaults_are_valid() -> None:
    assert validate_profile(Profile(), MODELS).valid


def test_visual_index_cannot_serve_broad_search() -> None:
    result = validate_profile(
        Profile(
            ingest_mode=IngestMode.MULTIMODAL,
            query_mode=QueryMode.GLOBAL,
            chat_model="qwen2.5vl:7b",
        ),
        MODELS,
    )
    assert not result.valid
    assert "query_mode" in errors(result)


def test_visual_index_requires_a_vision_model() -> None:
    result = validate_profile(
        Profile(
            ingest_mode=IngestMode.MULTIMODAL,
            query_mode=QueryMode.LOCAL,
            chat_model="qwen3:8b",
        ),
        MODELS,
    )
    assert not result.valid
    assert "chat_model" in errors(result)


def test_visual_index_with_vision_model_is_valid() -> None:
    result = validate_profile(
        Profile(
            ingest_mode=IngestMode.MULTIMODAL,
            query_mode=QueryMode.LOCAL,
            chat_model="qwen2.5vl:7b",
        ),
        MODELS,
    )
    assert result.valid


def test_focused_search_needs_an_embedding_model() -> None:
    result = validate_profile(
        Profile(query_mode=QueryMode.LOCAL, embedding_model=""), MODELS
    )
    assert not result.valid
    assert "embedding_model" in errors(result)


def test_broad_search_survives_a_missing_embedding_model() -> None:
    # Global search map-reduces over community reports and never embeds.
    result = validate_profile(
        Profile(query_mode=QueryMode.GLOBAL, embedding_model=""), MODELS
    )
    assert result.valid


def test_agentic_broad_warns_but_is_allowed() -> None:
    result = validate_profile(
        Profile(rag_mode=RagMode.AGENTIC, query_mode=QueryMode.GLOBAL), MODELS
    )
    assert result.valid
    assert any(i.level is IssueLevel.WARNING and i.field == "rag_mode" for i in result.issues)


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    result = validate_profile(Profile(chunk_size=400, chunk_overlap=400), MODELS)
    assert not result.valid
    assert "chunk_overlap" in errors(result)


def test_large_chunks_warn_about_entity_recall() -> None:
    result = validate_profile(Profile(chunk_size=2400), MODELS)
    assert result.valid
    assert any(i.field == "chunk_size" for i in result.issues)


def test_semantic_chunking_warns_for_text_ingestion() -> None:
    result = validate_profile(Profile(chunker=Chunker.SEMANTIC), MODELS)
    assert result.valid
    assert any(i.field == "chunker" for i in result.issues)


def test_unavailable_model_is_an_error() -> None:
    models = dict(MODELS)
    models["gpt-4o"] = ModelInfo(
        id="gpt-4o", provider=Provider.OPENAI, label="GPT-4o",
        available=False, unavailable_reason="No API key.",
    )
    result = validate_profile(Profile(chat_model="gpt-4o"), models)
    assert not result.valid
    assert "chat_model" in errors(result)


def test_medium_effort_requires_decision_model() -> None:
    result = validate_profile(
        Profile(ingest_effort=IngestEffort.MEDIUM, chunk_decision_model=""),
        MODELS,
    )
    assert not result.valid
    assert "chunk_decision_model" in errors(result)


def test_medium_effort_with_decision_model_is_valid() -> None:
    result = validate_profile(
        Profile(
            ingest_effort=IngestEffort.MEDIUM,
            chunk_decision_model="qwen2.5:3b",
        ),
        MODELS,
    )
    assert result.valid


def test_low_effort_warns_when_manual_chunker_differs() -> None:
    result = validate_profile(
        Profile(ingest_effort=IngestEffort.LOW, chunker=Chunker.RECURSIVE),
        MODELS,
    )
    assert result.valid
    assert any(i.field == "chunker" for i in result.issues)


def test_multimodal_warns_on_non_manual_effort() -> None:
    result = validate_profile(
        Profile(
            ingest_mode=IngestMode.MULTIMODAL,
            ingest_effort=IngestEffort.LOW,
            chat_model="qwen2.5vl:7b",
        ),
        MODELS,
    )
    assert result.valid
    assert any(i.field == "ingest_effort" for i in result.issues)


def test_model_metrics_online_round_trip() -> None:
    """Profile field used by Settings online-metrics toggle persists through dump/load."""
    raw = Profile(model_metrics_online=True).model_dump_json()
    restored = Profile.model_validate_json(raw)
    assert restored.model_metrics_online is True
    assert Profile().model_metrics_online is False
