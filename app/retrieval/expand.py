"""Parent-section expansion and citation mapping for retrieved chunks."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from app.cache import get_note_cache, section_bounds
from app.models import Citation
from app.security import get_policy_engine


def expand_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grow chunks to enclosing heading sections from disk when policy allows."""
    policy = get_policy_engine()
    vault = policy.vault_path
    if not vault:
        return chunks
    cache = get_note_cache()
    out: list[dict[str, Any]] = []
    for chunk in chunks:
        path = chunk.get("note_path") or ""
        if not path:
            out.append(chunk)
            continue
        try:
            full = (vault / path).resolve()
            verdict = policy.check("vault_read", path=full, mode="read")
            if not verdict.allowed:
                out.append(chunk)
                continue
            text = cache.read(full)
            start = int(chunk.get("char_start") or 0)
            s, e = section_bounds(text, start)
            row = dict(chunk)
            row["text"] = text[s:e]
            row["char_start"] = s
            row["char_end"] = e
            out.append(row)
        except Exception:  # noqa: BLE001
            out.append(chunk)
    return out


def chunk_to_citation(chunk: dict[str, Any]) -> Citation:
    """Map a retrieved chunk dict to a Citation DTO."""
    return Citation(
        id=str(chunk.get("id") or uuid.uuid4()),
        note_path=str(chunk.get("note_path") or ""),
        note_title=str(
            chunk.get("note_title") or Path(str(chunk.get("note_path") or "")).stem
        ),
        heading_path=list(chunk.get("heading_path") or []),
        snippet=(chunk.get("text") or "")[:280],
        char_start=int(chunk.get("char_start") or 0),
        char_end=int(chunk.get("char_end") or 0),
        score=float(chunk.get("score") or 0.0),
        source=chunk.get("source") or "vector",  # type: ignore[arg-type]
    )
