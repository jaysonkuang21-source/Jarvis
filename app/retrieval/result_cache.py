"""TTL cache for ranked retrieval results."""

from __future__ import annotations

import hashlib
import json
import time
from threading import Lock
from typing import Any

from app.config import get_settings
from app.models import Profile, QueryMode
from app.retrieval.hybrid import MetadataFilter


class RetrievalResultCache:
    """Process-local cache of post-rerank chunk lists keyed by query context."""

    def __init__(self, ttl_seconds: int | None = None) -> None:
        """Use settings TTL when not overridden."""
        self._ttl = (
            get_settings().cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        )
        self._entries: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._lock = Lock()

    def get(self, key: str) -> list[dict[str, Any]] | None:
        """Return a deep-ish copy of cached chunks when still fresh."""
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            expires_at, chunks = hit
            if now >= expires_at:
                del self._entries[key]
                return None
            return [dict(c) for c in chunks]

    def set(self, key: str, chunks: list[dict[str, Any]]) -> None:
        """Store chunks until TTL elapses."""
        with self._lock:
            self._entries[key] = (
                time.monotonic() + max(1, self._ttl),
                [dict(c) for c in chunks],
            )

    def clear(self) -> None:
        """Drop every cached retrieval result."""
        with self._lock:
            self._entries.clear()


def cache_key(
    question: str,
    profile: Profile,
    mode: QueryMode,
    filters: MetadataFilter | None = None,
) -> str:
    """Hash profile retrieval knobs, mode, filters, and the question."""
    filt = filters or MetadataFilter()
    payload = {
        "q": question,
        "mode": mode.value,
        "filters": {"path_prefix": filt.path_prefix, "tags": list(filt.tags)},
        "embedding_model": profile.embedding_model,
        "top_k": profile.top_k,
        "rrf_k": profile.rrf_k,
        "hybrid_vector_top_k": profile.hybrid_vector_top_k,
        "hybrid_keyword_top_k": profile.hybrid_keyword_top_k,
        "rerank_model": profile.rerank_model,
        "query_mode": profile.query_mode.value,
        "rag_mode": profile.rag_mode.value,
        "community_level": profile.community_level,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_result_cache: RetrievalResultCache | None = None


def get_retrieval_cache() -> RetrievalResultCache:
    """Return the process-wide retrieval result cache."""
    global _result_cache
    if _result_cache is None:
        _result_cache = RetrievalResultCache()
    return _result_cache
