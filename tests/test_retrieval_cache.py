"""Retrieval result cache and metadata filter helpers."""

from __future__ import annotations

from app.models import Profile, QueryMode
from app.retrieval.hybrid import MetadataFilter
from app.retrieval.result_cache import RetrievalResultCache, cache_key


def test_result_cache_round_trip() -> None:
    cache = RetrievalResultCache(ttl_seconds=60)
    key = "abc"
    chunks = [{"id": 1, "text": "hello", "score": 0.9}]
    assert cache.get(key) is None
    cache.set(key, chunks)
    hit = cache.get(key)
    assert hit is not None
    assert hit[0]["id"] == 1
    hit[0]["text"] = "mutated"
    assert cache.get(key)[0]["text"] == "hello"


def test_cache_key_stable_for_same_inputs() -> None:
    profile = Profile()
    filt = MetadataFilter(path_prefix="notes/", tags=("a",))
    a = cache_key("q", profile, QueryMode.LOCAL, filt)
    b = cache_key("q", profile, QueryMode.LOCAL, filt)
    assert a == b
    c = cache_key("other", profile, QueryMode.LOCAL, filt)
    assert a != c


def test_metadata_filter_kwargs() -> None:
    empty = MetadataFilter()
    assert empty.as_kwargs() == {"path_prefix": None, "tags": None}
    filled = MetadataFilter(path_prefix="proj", tags=("x", "y"))
    assert filled.as_kwargs()["path_prefix"] == "proj"
    assert filled.as_kwargs()["tags"] == ["x", "y"]
