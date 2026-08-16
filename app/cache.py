"""Note content cache and LLM response deduplication cache.



``NoteCache`` is keyed on path and mtime for citation previews and parent

expansion. ``ResponseCache`` stores full assistant replies (text + citations)

with TTL so repeat questions can skip generation; prefer Redis in

multi-instance deployments. Entries with non-empty chat history are never

looked up or stored (answers are thread-dependent).

"""



from __future__ import annotations



import hashlib

import json

import time

from collections import OrderedDict

from pathlib import Path

from threading import Lock

from typing import TYPE_CHECKING, Any, TypedDict



if TYPE_CHECKING:

    from app.models import Profile





class CachedResponse(TypedDict):

    """One successful chat completion stored for a later cache hit."""



    response: str

    citations: list[dict[str, Any]]





class NoteCache:

    def __init__(self, max_entries: int = 256) -> None:

        """Create an LRU cache capped at ``max_entries`` note texts."""

        self._max = max_entries

        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()

        self._lock = Lock()



    def read(self, path: Path) -> str:

        """Return note text, reusing a cached copy when mtime is unchanged.



        Thread-safe: the lock is held only around map access, not the disk read,

        so concurrent misses can race and both write — last write wins.

        """

        key = str(path)

        mtime = path.stat().st_mtime



        with self._lock:

            cached = self._entries.get(key)

            if cached is not None and cached[0] == mtime:

                self._entries.move_to_end(key)

                return cached[1]



        text = path.read_text(encoding="utf-8", errors="replace")



        with self._lock:

            self._entries[key] = (mtime, text)

            self._entries.move_to_end(key)

            while len(self._entries) > self._max:

                self._entries.popitem(last=False)

        return text



    def clear(self) -> None:

        """Drop every cached note under the lock."""

        with self._lock:

            self._entries.clear()





def section_bounds(text: str, char_start: int) -> tuple[int, int]:

    """Bounds of the markdown heading section containing ``char_start``.



    This is the parent expansion: a retrieved chunk grows to its enclosing

    section by reading the note, rather than by maintaining a second index of

    parent documents that has to be kept in sync.

    """

    if not text:

        return 0, 0



    char_start = max(0, min(char_start, len(text) - 1))

    offsets: list[tuple[int, int]] = []

    cursor = 0

    for line in text.splitlines(keepends=True):

        if line.lstrip().startswith("#"):

            offsets.append((cursor, len(line) - len(line.lstrip())))

        cursor += len(line)



    start = 0

    end = len(text)

    for index, (offset, _) in enumerate(offsets):

        if offset <= char_start:

            start = offset

            end = offsets[index + 1][0] if index + 1 < len(offsets) else len(text)

        else:

            break

    return start, end





def _normalize_message(message: str) -> str:

    """Collapse surrounding whitespace for cache keys; preserve case."""

    return " ".join(message.split())





def response_cache_key(

    message: str,

    profile: Profile,

    *,

    index_fingerprint: str | None = None,

) -> str:

    """Hash message + profile chat/retrieval identity + optional index stamp.



    Message case is preserved; only whitespace is normalized. Callers that have

    non-empty history must not call get/set (answers depend on the thread).

    """

    payload = {

        "message": _normalize_message(message),

        "chat_model": profile.chat_model,

        "chat_provider": getattr(

            profile.chat_provider, "value", str(profile.chat_provider)

        ),

        "embedding_model": profile.embedding_model,

        "query_mode": getattr(profile.query_mode, "value", str(profile.query_mode)),

        "rag_mode": getattr(profile.rag_mode, "value", str(profile.rag_mode)),

        "top_k": profile.top_k,

        "community_level": profile.community_level,

        "rerank_model": profile.rerank_model,

        "max_context_tokens": profile.max_context_tokens,

        "index": index_fingerprint or "",

    }

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()





class ResponseCache:

    """In-memory LLM response cache with TTL and hit/miss counters.



    Expired entries are deleted and counted as misses (same as a cold miss).

    Keys include profile and index fingerprint so config changes cannot reuse

    a stale answer. Stored values are ``CachedResponse`` dicts, not bare strings.

    """



    def __init__(self, ttl_seconds: int = 300) -> None:

        """Create an empty cache that expires entries after ``ttl_seconds``."""

        self.ttl = ttl_seconds

        self._cache: dict[str, dict[str, Any]] = {}

        self._hits = 0

        self._misses = 0

        self._lock = Lock()



    def get(

        self,

        message: str,

        profile: Profile,

        *,

        index_fingerprint: str | None = None,

    ) -> CachedResponse | None:

        """Return a cached completion when present and unexpired; else None.



        On TTL expiry the key is removed and the access counts as a miss.

        """

        key = response_cache_key(

            message, profile, index_fingerprint=index_fingerprint

        )

        with self._lock:

            entry = self._cache.get(key)

            if entry is not None:

                if time.time() - float(entry["timestamp"]) < self.ttl:

                    self._hits += 1

                    return {

                        "response": str(entry["response"]),

                        "citations": list(entry.get("citations") or []),

                    }

                del self._cache[key]

            self._misses += 1

            return None



    def set(

        self,

        message: str,

        response: str,

        profile: Profile,

        *,

        index_fingerprint: str | None = None,

        citations: list[dict[str, Any]] | None = None,

    ) -> None:

        """Store a successful completion under the composite key.



        Callers must not store partial, errored, disconnected, or

        approval-required outcomes. Empty ``response`` should not be cached.

        """

        if not response:

            return

        key = response_cache_key(

            message, profile, index_fingerprint=index_fingerprint

        )

        with self._lock:

            self._cache[key] = {

                "response": response,

                "citations": list(citations or []),

                "timestamp": time.time(),

                "query": message,

            }



    @property

    def stats(self) -> dict[str, Any]:

        """Hit/miss rates and current occupancy for metrics and debugging."""

        total = self._hits + self._misses

        hit_rate = self._hits / total if total else 0.0

        with self._lock:

            cached_entries = len(self._cache)

        return {

            "hits": self._hits,

            "misses": self._misses,

            "hit_rate": f"{hit_rate:.2%}",

            "cached_entries": cached_entries,

        }



    def clear(self) -> None:

        """Drop all entries; leave hit/miss counters intact."""

        with self._lock:

            self._cache.clear()





_cache: NoteCache | None = None

_response_cache: ResponseCache | None = None





def get_note_cache() -> NoteCache:

    """Return the process-wide note cache, creating it on first use."""

    global _cache

    if _cache is None:

        _cache = NoteCache()

    return _cache





def get_response_cache() -> ResponseCache:

    """Return the process-wide response cache, creating it on first use."""

    global _response_cache

    if _response_cache is None:

        from app.config import get_settings



        _response_cache = ResponseCache(ttl_seconds=get_settings().cache_ttl_seconds)

    return _response_cache





def clear_answer_caches() -> None:

    """Drop response and retrieval result caches after index rebuilds."""

    get_response_cache().clear()

    try:

        from app.retrieval.result_cache import get_retrieval_cache



        get_retrieval_cache().clear()

    except Exception:  # noqa: BLE001 - retrieval may be optional at import time

        pass

