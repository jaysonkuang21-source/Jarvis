"""Hybrid retrieval: vector + keyword + reciprocal rank fusion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.db import repo
from app.models import Profile


@dataclass(frozen=True)
class MetadataFilter:
    """Optional document-level constraints applied before hybrid search."""

    path_prefix: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def as_kwargs(self) -> dict[str, Any]:
        """Keyword args for repo vector/keyword search helpers."""
        return {
            "path_prefix": self.path_prefix,
            "tags": list(self.tags) if self.tags else None,
        }

    def cache_tuple(self) -> tuple[str | None, tuple[str, ...]]:
        """Stable key fragment for the retrieval result cache."""
        return self.path_prefix, self.tags


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict[str, Any]]],
    *,
    k: int = 60,
    id_key: str = "id",
) -> list[dict[str, Any]]:
    """Fuse ranked result lists with RRF: score = Σ 1/(k + rank)."""
    scores: dict[Any, float] = {}
    payloads: dict[Any, dict[str, Any]] = {}
    for results in ranked_lists:
        for rank, item in enumerate(results, start=1):
            key = item[id_key]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            payloads[key] = item
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    out: list[dict[str, Any]] = []
    for key, score in ordered:
        row = dict(payloads[key])
        row["score"] = score
        row["source"] = "vector"
        out.append(row)
    return out


def hybrid_search(
    query: str,
    embedding: list[float] | None,
    profile: Profile,
    *,
    filters: MetadataFilter | None = None,
) -> list[dict[str, Any]]:
    """Run metadata-filtered vector + FTS searches and fuse with RRF."""
    filt = filters or MetadataFilter()
    filter_kwargs = filt.as_kwargs()
    vector_hits: list[dict[str, Any]] = []
    if embedding is not None:
        vector_hits = repo.vector_search(
            embedding, profile.hybrid_vector_top_k, **filter_kwargs
        )
    keyword_hits = repo.keyword_search(
        query, profile.hybrid_keyword_top_k, **filter_kwargs
    )
    fused = reciprocal_rank_fusion(
        [vector_hits, keyword_hits],
        k=profile.rrf_k,
    )
    return fused[: profile.top_k]
