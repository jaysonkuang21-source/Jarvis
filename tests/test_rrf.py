"""Unit tests for reciprocal rank fusion."""

from __future__ import annotations

from app.retrieval.hybrid import reciprocal_rank_fusion


def test_rrf_prefers_items_high_in_multiple_lists() -> None:
    a = [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]
    b = [{"id": 2, "text": "b"}, {"id": 3, "text": "c"}]
    fused = reciprocal_rank_fusion([a, b], k=60)
    assert fused[0]["id"] == 2
    assert fused[0]["score"] > fused[1]["score"]


def test_rrf_empty() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []
