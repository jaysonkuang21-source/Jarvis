"""Hybrid retrieval tests with mocked repo searches."""

from __future__ import annotations

from typing import Any

import pytest

from app.models import Profile
from app.retrieval.hybrid import hybrid_search


def test_hybrid_search_fuses_and_truncates(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RRF should prefer items high in both lists and respect top_k."""

    def vector_search(
        _embedding: list[float], limit: int, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        assert limit == profile.hybrid_vector_top_k
        return [
            {"id": 1, "text": "shared"},
            {"id": 2, "text": "vector-only"},
        ]

    def keyword_search(
        _query: str, limit: int, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        assert limit == profile.hybrid_keyword_top_k
        return [
            {"id": 1, "text": "shared"},
            {"id": 3, "text": "keyword-only"},
        ]

    monkeypatch.setattr("app.retrieval.hybrid.repo.vector_search", vector_search)
    monkeypatch.setattr("app.retrieval.hybrid.repo.keyword_search", keyword_search)

    profile = profile.model_copy(update={"top_k": 2, "rrf_k": 60})
    hits = hybrid_search("alpha", [0.1, 0.2], profile)
    assert len(hits) == 2
    assert hits[0]["id"] == 1
    assert "score" in hits[0]
    assert hits[0]["score"] > hits[1]["score"]


def test_hybrid_search_without_embedding_uses_keywords_only(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When embedding is None, skip vector search and return keyword hits."""

    def vector_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("vector_search must not run without an embedding")

    def keyword_search(_query: str, _limit: int, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": 9, "text": "kw"}]

    monkeypatch.setattr("app.retrieval.hybrid.repo.vector_search", vector_search)
    monkeypatch.setattr("app.retrieval.hybrid.repo.keyword_search", keyword_search)

    hits = hybrid_search("beta", None, profile)
    assert len(hits) == 1
    assert hits[0]["id"] == 9
    assert hits[0]["score"] > 0
