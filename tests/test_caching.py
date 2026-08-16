"""NoteCache, ResponseCache, and section_bounds tests."""

from __future__ import annotations

import time
from pathlib import Path

from app.cache import NoteCache, ResponseCache, clear_answer_caches, section_bounds

MARKDOWN = """# Intro

Opening paragraph.

## Details

Body of details.

## Closing

End notes.
"""


def test_section_bounds_empty() -> None:
    assert section_bounds("", 0) == (0, 0)


def test_section_bounds_selects_heading_section() -> None:
    start, end = section_bounds(MARKDOWN, MARKDOWN.index("Body of details"))
    section = MARKDOWN[start:end]
    assert section.startswith("## Details")
    assert "Body of details" in section
    assert "## Closing" not in section


def test_section_bounds_clamps_out_of_range() -> None:
    start, end = section_bounds(MARKDOWN, 10_000)
    assert 0 <= start < end <= len(MARKDOWN)


def test_note_cache_hit_and_mtime_invalidation(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("first", encoding="utf-8")
    cache = NoteCache(max_entries=8)

    assert cache.read(path) == "first"
    assert cache.read(path) == "first"

    time.sleep(0.02)
    path.write_text("second", encoding="utf-8")
    assert cache.read(path) == "second"


def test_note_cache_lru_eviction(tmp_path: Path) -> None:
    cache = NoteCache(max_entries=2)
    paths = []
    for name in ("a.md", "b.md", "c.md"):
        path = tmp_path / name
        path.write_text(name, encoding="utf-8")
        paths.append(path)
        cache.read(path)

    # Touch a so it is recently used; c insertion already evicted a if unused —
    # re-read a and ensure c still loads (evicts b).
    assert cache.read(paths[0]) == "a.md"
    assert cache.read(paths[2]) == "c.md"
    assert len(cache._entries) == 2


def test_note_cache_clear(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_text("x", encoding="utf-8")
    cache = NoteCache()
    cache.read(path)
    assert cache._entries
    cache.clear()
    assert not cache._entries


def test_response_cache_hit_and_miss() -> None:
    from app.models import Profile

    profile = Profile()
    cache = ResponseCache(ttl_seconds=60)
    assert cache.get("What is Alpha?", profile) is None
    cache.set("What is Alpha?", "Alpha is a project.", profile)
    hit = cache.get("What is Alpha?", profile)
    assert hit is not None
    assert hit["response"] == "Alpha is a project."
    assert hit["citations"] == []
    # Whitespace normalize only — case must differ.
    assert cache.get("  What is Alpha?  ", profile) is not None
    assert cache.get("  what is alpha?  ", profile) is None
    stats = cache.stats
    assert stats["hits"] == 2
    assert stats["misses"] == 2
    assert stats["cached_entries"] == 1


def test_response_cache_stores_citations() -> None:
    """Cache hits must restore citation payloads, not bare strings."""
    from app.models import Profile

    profile = Profile()
    cache = ResponseCache(ttl_seconds=60)
    citations = [
        {
            "id": "c1",
            "note_path": "Alpha.md",
            "note_title": "Alpha",
            "heading_path": [],
            "snippet": "snippet",
            "char_start": 0,
            "char_end": 7,
            "score": 0.9,
            "source": "vector",
            "page": None,
        }
    ]
    cache.set("q", "answer", profile, citations=citations)
    hit = cache.get("q", profile)
    assert hit is not None
    assert hit["response"] == "answer"
    assert hit["citations"] == citations


def test_response_cache_refuses_empty_response() -> None:
    from app.models import Profile

    profile = Profile()
    cache = ResponseCache(ttl_seconds=60)
    cache.set("q", "", profile)
    assert cache.get("q", profile) is None
    assert cache.stats["cached_entries"] == 0


def test_response_cache_separates_by_profile() -> None:
    """Same question under different chat models must not share a cache entry."""
    from app.models import Profile

    cache = ResponseCache(ttl_seconds=60)
    a = Profile(chat_model="model-a")
    b = Profile(chat_model="model-b")
    cache.set("Shared question?", "answer-a", a)
    hit = cache.get("Shared question?", a)
    assert hit is not None and hit["response"] == "answer-a"
    assert cache.get("Shared question?", b) is None


def test_response_cache_separates_by_index_fingerprint() -> None:
    """Reindex fingerprint must invalidate cached answers."""
    from app.models import Profile

    cache = ResponseCache(ttl_seconds=60)
    profile = Profile()
    cache.set("q", "old", profile, index_fingerprint="v1")
    hit = cache.get("q", profile, index_fingerprint="v1")
    assert hit is not None and hit["response"] == "old"
    assert cache.get("q", profile, index_fingerprint="v2") is None


def test_response_cache_expired_counts_as_miss() -> None:
    from app.cache import response_cache_key
    from app.models import Profile

    profile = Profile()
    cache = ResponseCache(ttl_seconds=1)
    cache.set("q", "answer", profile)
    # Force expiry without sleeping a full second of wall clock beyond tolerance.
    key = response_cache_key("q", profile)
    cache._cache[key]["timestamp"] = time.time() - 10
    assert cache.get("q", profile) is None
    assert key not in cache._cache
    assert cache.stats["misses"] == 1
    assert cache.stats["hits"] == 0


def test_response_cache_cold_miss_increments_miss_counter() -> None:
    """A never-seen key is a miss and leaves the cache empty."""
    from app.models import Profile

    profile = Profile()
    cache = ResponseCache(ttl_seconds=60)
    assert cache.get("brand new question", profile) is None
    assert cache.get("another unseen query", profile) is None
    assert cache.stats["misses"] == 2
    assert cache.stats["hits"] == 0
    assert cache.stats["cached_entries"] == 0


def test_response_cache_miss_then_set_then_hit() -> None:
    """Miss → set → hit follows the ResponseCache contract used by chat."""
    from app.models import Profile

    profile = Profile()
    cache = ResponseCache(ttl_seconds=300)
    query = "Summarize Project Beta"

    assert cache.get(query, profile) is None
    assert cache.stats["misses"] == 1

    cache.set(query, "Beta is the sibling note.", profile)
    hit = cache.get(query, profile)
    assert hit is not None
    assert hit["response"] == "Beta is the sibling note."
    assert cache.stats["hits"] == 1
    assert cache.stats["misses"] == 1


def test_clear_answer_caches_empties_response_and_retrieval() -> None:
    """Successful reindex path clears both response and retrieval caches."""
    from app.cache import get_response_cache
    from app.models import Profile
    from app.retrieval.result_cache import get_retrieval_cache

    rc = get_response_cache()
    rc.clear()
    get_retrieval_cache().clear()
    rc.set("q", "a", Profile())
    get_retrieval_cache().set("k", [{"id": "1"}])
    clear_answer_caches()
    assert rc.stats["cached_entries"] == 0
    assert get_retrieval_cache().get("k") is None


def test_metrics_collector_records_cache_miss() -> None:
    """record_request(cache_hit=False) feeds /api/metrics cache_hit_rate."""
    from app.monitoring import MetricsCollector

    metrics = MetricsCollector()
    metrics.record_request(12.0, cache_hit=False)
    metrics.record_request(8.0, cache_hit=False)
    metrics.record_request(5.0, cache_hit=True)

    assert metrics.metrics["cache_misses"] == 2
    assert metrics.metrics["cache_hits"] == 1
    assert metrics.summary["cache_hit_rate"] == 0.3333
