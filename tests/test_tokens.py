"""Token recording and metrics collector unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.ingestion import embeddings as emb_mod
from app.ingestion.chunkers import apply_chunker, estimate_tokens
from app.models import Chunker, Profile
from app.monitoring import MetricsCollector, get_metrics


def test_record_tokens_accumulates_without_request() -> None:
    metrics = MetricsCollector()
    metrics.record_tokens(input_tokens=10, output_tokens=4)
    metrics.record_tokens(input_tokens=5)
    assert metrics.metrics["tokens_input"] == 15
    assert metrics.metrics["tokens_output"] == 4
    assert metrics.metrics["total_requests"] == 0


@pytest.mark.asyncio
async def test_embed_query_records_input_tokens(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stub:
        async def aembed_query(self, text: str) -> list[float]:
            """Return a fixed vector."""
            return [0.1, 0.2]

    monkeypatch.setattr(emb_mod, "build_embeddings", lambda *_a, **_k: Stub())
    metrics = get_metrics()
    before = int(metrics.metrics["tokens_input"])
    text = "hello embeddings"
    await emb_mod.embed_query(profile, text)
    assert int(metrics.metrics["tokens_input"]) == before + estimate_tokens(text)


@pytest.mark.asyncio
async def test_embed_documents_records_input_tokens(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stub:
        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            """Return one vector per text."""
            return [[float(i)] for i, _ in enumerate(texts)]

    monkeypatch.setattr(emb_mod, "build_embeddings", lambda *_a, **_k: Stub())
    metrics = get_metrics()
    before = int(metrics.metrics["tokens_input"])
    texts = ["alpha", "bravo charlie"]
    await emb_mod.embed_documents(profile, texts)
    expected = sum(estimate_tokens(t) for t in texts)
    assert int(metrics.metrics["tokens_input"]) == before + expected


def test_apply_chunker_records_input_and_output_tokens(profile: Profile) -> None:
    profile = profile.model_copy(
        update={
            "chunker": Chunker.RECURSIVE,
            "chunk_size": 128,
            "chunk_overlap": 20,
            "prepend_note_context": False,
        }
    )
    text = "# Title\n\nA short paragraph for chunk token accounting."
    metrics = get_metrics()
    before_in = int(metrics.metrics["tokens_input"])
    before_out = int(metrics.metrics["tokens_output"])

    chunks = apply_chunker(Chunker.RECURSIVE, text, profile, title="Title")
    assert chunks

    assert int(metrics.metrics["tokens_input"]) == before_in + estimate_tokens(text)
    expected_out = sum(estimate_tokens(c.text) for c in chunks)
    assert int(metrics.metrics["tokens_output"]) == before_out + expected_out


@pytest.mark.asyncio
async def test_generator_path_records_prompt_and_completion_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Placeholder engine generation records prompt (in) and streamed (out) tokens."""
    from app.agent import PlaceholderRetrievalEngine, get_registry
    from app.models import Profile, TokenEvent

    class FakeModel:
        async def astream(self, _messages: Any):
            """Yield two token chunks then stop."""

            class Part:
                content = "Hello "

            class Part2:
                content = "world"

            yield Part()
            yield Part2()

    monkeypatch.setattr(
        "app.agent.build_chat_model",
        lambda _model, _provider, **_kwargs: FakeModel(),
    )
    engine = PlaceholderRetrievalEngine(get_registry())
    profile = Profile()
    metrics = get_metrics()
    before_in = int(metrics.metrics["tokens_input"])
    before_out = int(metrics.metrics["tokens_output"])

    tokens: list[str] = []
    async for event in engine.query("What is Jarvis?", profile, history=[]):
        if isinstance(event, TokenEvent):
            tokens.append(event.text)

    assert "".join(tokens) == "Hello world"
    assert int(metrics.metrics["tokens_input"]) > before_in
    assert int(metrics.metrics["tokens_output"]) == before_out + estimate_tokens(
        "Hello world"
    )
