"""Chunking strategies for vault notes.

Uses LangChain text splitters + tiktoken for recursive/structure/semantic paths,
and Chonkie's sentence chunker for claim-centered units. Char offsets are into
the text passed to the chunker (typically prepared working text); callers map
them back to on-disk spans when needed.

See ``docs/ingestion-plan.md``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from langchain_text_splitters import (
    Language,
    RecursiveCharacterTextSplitter,
)
from pydantic import BaseModel, Field

from app.models import Chunker, Profile

_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

_MD_HEADERS: list[tuple[str, str]] = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
    ("####", "h4"),
    ("#####", "h5"),
    ("######", "h6"),
]


class EvidenceChunk(BaseModel):
    """One evidence span ready for vector upsert and citation."""

    text: str
    char_start: int
    char_end: int
    heading_path: list[str] = Field(default_factory=list)
    chunk_id: str = ""
    wikilinks: list[str] = Field(default_factory=list)
    document_id: str | None = None
    doc_title: str | None = None
    section: str = ""
    page: int | None = None
    tags: list[str] = Field(default_factory=list)


@lru_cache(maxsize=1)
def _tiktoken_encoding():
    """Load the shared cl100k encoding once."""
    import tiktoken

    return tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str) -> int:
    """Count tokens with tiktoken (cl100k_base)."""
    if not text.strip():
        return 0
    return len(_tiktoken_encoding().encode(text))


def extract_wikilinks(text: str) -> list[str]:
    """Return unique ``[[target]]`` strings in document order."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _WIKILINK_RE.finditer(text):
        link = match.group(0)
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out


def _markdown_sections(text: str) -> list[tuple[list[str], int, int]]:
    """Locate header sections by scanning heading lines (offset-accurate).

    Avoids round-tripping through MarkdownHeaderTextSplitter body text, which
    can normalize punctuation and make ``str.find`` miss later sections.
    """
    if not text.strip():
        return []

    headers = [
        (match.start(), len(match.group(1)), match.group(2).strip())
        for match in _HEADER_RE.finditer(text)
    ]
    if not headers:
        return [([], 0, len(text))]

    sections: list[tuple[list[str], int, int]] = []
    first_start = headers[0][0]
    if first_start > 0 and text[:first_start].strip():
        sections.append(([], 0, first_start))

    stack: list[tuple[int, str]] = []
    for index, (start, level, title) in enumerate(headers):
        end = headers[index + 1][0] if index + 1 < len(headers) else len(text)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = [name for _lvl, name in stack]
        sections.append((path, start, end))
    return sections


def _heading_path_for_span(
    sections: list[tuple[list[str], int, int]], start: int
) -> list[str]:
    """Pick the heading path of the section that owns ``start``."""
    chosen: list[str] = []
    for path, sec_start, sec_end in sections:
        if sec_start <= start < sec_end:
            chosen = path
        elif start >= sec_end:
            chosen = path
    return list(chosen)


def _recursive_splitter(profile: Profile) -> RecursiveCharacterTextSplitter:
    """Markdown-aware recursive splitter sized from the profile."""
    return RecursiveCharacterTextSplitter.from_language(
        Language.MARKDOWN,
        chunk_size=profile.chunk_size,
        chunk_overlap=profile.chunk_overlap,
        length_function=estimate_tokens,
    )


def _protect_wikilink_cuts(text: str, start: int, end: int) -> tuple[int, int]:
    """Nudge window edges so a cut never lands inside ``[[...]]``."""
    for match in _WIKILINK_RE.finditer(text):
        link_start, link_end = match.start(), match.end()
        if start > link_start and start < link_end:
            start = link_start
        if end > link_start and end < link_end:
            end = link_end
    return start, end


def _locate_pieces(text: str, pieces: list[str]) -> list[tuple[int, int, str]]:
    """Map splitter outputs back to offsets in ``text`` via sequential search."""
    located: list[tuple[int, int, str]] = []
    search_from = 0
    for piece in pieces:
        if not piece.strip():
            continue
        pos = text.find(piece, search_from)
        if pos < 0:
            pos = text.find(piece)
        if pos < 0:
            continue
        located.append((pos, pos + len(piece), piece))
        # Advance past the start so overlaps still match later windows.
        search_from = pos + 1
    return located


def _finalize(
    text: str,
    spans: list[tuple[int, int, list[str]]],
    *,
    title: str | None,
    prepend_context: bool,
    protect_links: bool,
    document_id: str | None = None,
    tags: list[str] | None = None,
) -> list[EvidenceChunk]:
    """Build EvidenceChunk rows from absolute spans."""
    tag_list = list(tags or [])
    chunks: list[EvidenceChunk] = []
    for start, end, heading_path in spans:
        if protect_links:
            start, end = _protect_wikilink_cuts(text, start, end)
        piece = text[start:end]
        if not piece.strip():
            continue
        prefix = ""
        if prepend_context:
            # Avoid duplicating the note title when it is already the H1 path root.
            path_parts = list(heading_path)
            if title and path_parts and path_parts[0] == title:
                parts = path_parts
            else:
                parts = [p for p in [title, *path_parts] if p]
            if parts:
                prefix = f"[{' > '.join(parts)}]\n"
        section = heading_path[-1] if heading_path else ""
        chunks.append(
            EvidenceChunk(
                text=prefix + piece,
                char_start=start,
                char_end=end,
                heading_path=list(heading_path),
                wikilinks=extract_wikilinks(piece),
                document_id=document_id,
                doc_title=title,
                section=section,
                page=None,
                tags=tag_list,
            )
        )
    return [
        chunk.model_copy(update={"chunk_id": f"c{index:04d}"})
        for index, chunk in enumerate(chunks)
    ]


def chunk_recursive(
    text: str,
    profile: Profile,
    *,
    title: str | None = None,
    document_id: str | None = None,
    tags: list[str] | None = None,
) -> list[EvidenceChunk]:
    """LangChain markdown recursive split with tiktoken budgets."""
    if not text.strip():
        return []
    sections = _markdown_sections(text)
    splitter = _recursive_splitter(profile)
    spans: list[tuple[int, int, list[str]]] = []
    for path, sec_start, sec_end in sections:
        section = text[sec_start:sec_end]
        for rel_start, rel_end, _piece in _locate_pieces(
            section, splitter.split_text(section)
        ):
            spans.append((sec_start + rel_start, sec_start + rel_end, path))
    if not spans:
        # No header sections located; split the whole note.
        for start, end, _piece in _locate_pieces(text, splitter.split_text(text)):
            spans.append((start, end, []))
    return _finalize(
        text,
        spans,
        title=title,
        prepend_context=profile.prepend_note_context,
        protect_links=False,
        document_id=document_id,
        tags=tags,
    )


def chunk_structure_entity(
    text: str,
    profile: Profile,
    *,
    title: str | None = None,
    document_id: str | None = None,
    tags: list[str] | None = None,
) -> list[EvidenceChunk]:
    """Markdown header sections + recursive split, never cutting wikilinks."""
    if not text.strip():
        return []
    sections = _markdown_sections(text)
    splitter = _recursive_splitter(profile)
    spans: list[tuple[int, int, list[str]]] = []
    for path, sec_start, sec_end in sections:
        section = text[sec_start:sec_end]
        pieces = splitter.split_text(section)
        if not pieces:
            spans.append((sec_start, sec_end, path))
            continue
        for rel_start, rel_end, _piece in _locate_pieces(section, pieces):
            spans.append((sec_start + rel_start, sec_start + rel_end, path))
    if not spans:
        for start, end, _piece in _locate_pieces(text, splitter.split_text(text)):
            spans.append((start, end, []))
    return _finalize(
        text,
        spans,
        title=title,
        prepend_context=profile.prepend_note_context,
        protect_links=True,
        document_id=document_id,
        tags=tags,
    )


def chunk_semantic(
    text: str,
    profile: Profile,
    *,
    title: str | None = None,
    embeddings: Any | None = None,
    document_id: str | None = None,
    tags: list[str] | None = None,
) -> list[EvidenceChunk]:
    """Split with LangChain ``SemanticChunker`` (embedding breakpoint detection).

    Uses ``langchain_experimental.text_splitter.SemanticChunker`` with the
    profile embedding model. When ``embeddings`` is omitted, builds one from
    ``profile.embedding_model`` / ``embedding_provider``. Falls back to
    recursive chunking only if an embedder cannot be constructed.
    """
    if not text.strip():
        return []

    embedder = embeddings
    if embedder is None:
        try:
            from app.ingestion.embeddings import build_embeddings
            from app.models import Provider

            embedder = build_embeddings(
                profile.embedding_model,
                Provider(profile.embedding_provider),
            )
        except Exception:  # noqa: BLE001
            from app.monitoring import logger

            logger.info(
                "Semantic chunking: no embeddings available; falling back to recursive"
            )
            return chunk_recursive(
                text, profile, title=title, document_id=document_id, tags=tags
            )

    from langchain_experimental.text_splitter import SemanticChunker

    # Character floor derived from the token budget so tiny sentence shards merge.
    min_chars = max(int(profile.chunk_size) * 3, 64)
    splitter = SemanticChunker(
        embedder,
        add_start_index=True,
        breakpoint_threshold_type="percentile",
        min_chunk_size=min_chars,
    )
    documents = splitter.create_documents([text])
    sections = _markdown_sections(text)
    spans = _spans_from_semantic_documents(text, documents, sections)
    if not spans:
        # Last resort if start_index / locate failed: recursive.
        return chunk_recursive(
            text, profile, title=title, document_id=document_id, tags=tags
        )
    return _finalize(
        text,
        spans,
        title=title,
        prepend_context=profile.prepend_note_context,
        protect_links=False,
        document_id=document_id,
        tags=tags,
    )


def _spans_from_semantic_documents(
    text: str,
    documents: list[Any],
    sections: list[tuple[list[str], int, int]],
) -> list[tuple[int, int, list[str]]]:
    """Map LangChain SemanticChunker documents to absolute spans in ``text``."""
    spans: list[tuple[int, int, list[str]]] = []
    pieces: list[str] = []
    for doc in documents:
        content = getattr(doc, "page_content", None) or ""
        if not str(content).strip():
            continue
        piece = str(content)
        meta = getattr(doc, "metadata", None) or {}
        start_raw = meta.get("start_index") if isinstance(meta, dict) else None
        if isinstance(start_raw, int) and start_raw >= 0:
            start = start_raw
            end = min(len(text), start + len(piece))
            # Prefer exact slice when the embedder truncated whitespace oddly.
            if text[start:end] != piece:
                found = text.find(piece, start)
                if found >= 0:
                    start, end = found, found + len(piece)
                else:
                    pieces.append(piece)
                    continue
            spans.append((start, end, _heading_path_for_span(sections, start)))
        else:
            pieces.append(piece)
    if pieces:
        for start, end, _piece in _locate_pieces(text, pieces):
            spans.append((start, end, _heading_path_for_span(sections, start)))
    spans.sort(key=lambda row: (row[0], row[1]))
    return spans


def chunk_claim_centered(
    text: str,
    profile: Profile,
    *,
    title: str | None = None,
    document_id: str | None = None,
    tags: list[str] | None = None,
) -> list[EvidenceChunk]:
    """Sentence-oriented chunks via Chonkie (claim-sized until an LLM path exists)."""
    if not text.strip():
        return []

    from chonkie import SentenceChunker

    # Character tokenizer stays offline; chunk_size is in characters here.
    char_size = max(profile.chunk_size * 4, 64)
    char_overlap = max(profile.chunk_overlap * 4, 0)
    chunker = SentenceChunker(
        tokenizer="character",
        chunk_size=char_size,
        chunk_overlap=min(char_overlap, char_size - 1) if char_size > 1 else 0,
    )
    sections = _markdown_sections(text)
    spans: list[tuple[int, int, list[str]]] = []
    for ch in chunker.chunk(text):
        start, end = int(ch.start_index), int(ch.end_index)
        if end <= start:
            continue
        spans.append((start, end, _heading_path_for_span(sections, start)))
    return _finalize(
        text,
        spans,
        title=title,
        prepend_context=profile.prepend_note_context,
        protect_links=True,
        document_id=document_id,
        tags=tags,
    )


def apply_chunker(
    chunker: Chunker,
    text: str,
    profile: Profile,
    *,
    title: str | None = None,
    embeddings: Any | None = None,
    document_id: str | None = None,
    tags: list[str] | None = None,
) -> list[EvidenceChunk]:
    """Dispatch to the concrete chunker and record input/output token estimates."""
    if chunker is Chunker.STRUCTURE_ENTITY:
        chunks = chunk_structure_entity(
            text, profile, title=title, document_id=document_id, tags=tags
        )
    elif chunker is Chunker.SEMANTIC:
        chunks = chunk_semantic(
            text,
            profile,
            title=title,
            embeddings=embeddings,
            document_id=document_id,
            tags=tags,
        )
    elif chunker is Chunker.CLAIM_CENTERED:
        chunks = chunk_claim_centered(
            text, profile, title=title, document_id=document_id, tags=tags
        )
    else:
        chunks = chunk_recursive(
            text, profile, title=title, document_id=document_id, tags=tags
        )

    from app.monitoring import get_metrics

    get_metrics().record_tokens(
        input_tokens=estimate_tokens(text),
        output_tokens=sum(estimate_tokens(c.text) for c in chunks),
    )
    return chunks
