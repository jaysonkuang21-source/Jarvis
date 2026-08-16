"""Map uploaded filenames to document kinds and retrieval strategies."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class DocumentKind(StrEnum):
    """Coarse type used for ingest conversion and vault tags."""

    TEXT = "text"
    IMAGE = "image"
    PDF = "pdf"
    DOCX = "docx"
    BINARY = "binary"


class RetrieverKind(StrEnum):
    """Which retrieval path should prefer this document.

    ``text-hybrid`` is the default Postgres ANN+FTS path.
    ``visual`` filters to image-derived notes (OCR today; ColPali later).
    ``binary-meta`` prefers metadata stubs for opaque files.
    """

    TEXT_HYBRID = "text-hybrid"
    VISUAL = "visual"
    BINARY_META = "binary-meta"


@dataclass(frozen=True)
class FormatSpec:
    """Resolved ingest + retrieve strategy for one filename."""

    kind: DocumentKind
    retriever: RetrieverKind
    suffix: str
    mime: str
    searchable: bool
    note: str


_TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".py",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".log",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
    ".rst",
    ".tex",
    ".bib",
}

_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
    ".svg",
}

_PDF_SUFFIXES = {".pdf"}

_DOCX_SUFFIXES = {".docx"}
_DOCX_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def kind_tag(kind: DocumentKind) -> str:
    """Stable vault tag for document kind filtering."""
    return f"kind-{kind.value}"


def retriever_tag(retriever: RetrieverKind) -> str:
    """Stable vault tag naming the preferred retriever."""
    return f"retriever-{retriever.value}"


def resolve_format(filename: str, *, mime: str | None = None) -> FormatSpec:
    """Pick kind + retriever from filename suffix and optional MIME hint."""
    suffix = Path(filename or "file").suffix.lower()
    guessed, _ = mimetypes.guess_type(filename or "")
    resolved_mime = (mime or guessed or "application/octet-stream").lower()

    if suffix in _TEXT_SUFFIXES or resolved_mime.startswith("text/"):
        return FormatSpec(
            kind=DocumentKind.TEXT,
            retriever=RetrieverKind.TEXT_HYBRID,
            suffix=suffix or ".txt",
            mime=resolved_mime if resolved_mime != "application/octet-stream" else "text/plain",
            searchable=True,
            note="Plain/text source → text-hybrid retriever.",
        )
    if suffix in _IMAGE_SUFFIXES or resolved_mime.startswith("image/"):
        return FormatSpec(
            kind=DocumentKind.IMAGE,
            retriever=RetrieverKind.VISUAL,
            suffix=suffix or ".png",
            mime=resolved_mime if resolved_mime.startswith("image/") else "image/png",
            searchable=True,
            note="Image → visual retriever (OCR text index until ColPali).",
        )
    if suffix in _PDF_SUFFIXES or resolved_mime == "application/pdf":
        return FormatSpec(
            kind=DocumentKind.PDF,
            retriever=RetrieverKind.TEXT_HYBRID,
            suffix=".pdf",
            mime="application/pdf",
            searchable=True,
            note="PDF → pypdf text extract → text-hybrid retriever.",
        )
    if suffix in _DOCX_SUFFIXES or resolved_mime in _DOCX_MIMES:
        return FormatSpec(
            kind=DocumentKind.DOCX,
            retriever=RetrieverKind.TEXT_HYBRID,
            suffix=".docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            searchable=True,
            note="DOCX → OOXML text extract → text-hybrid retriever.",
        )
    return FormatSpec(
        kind=DocumentKind.BINARY,
        retriever=RetrieverKind.BINARY_META,
        suffix=suffix or ".bin",
        mime=resolved_mime,
        searchable=False,
        note="Opaque binary stored with a metadata note.",
    )


# Spoken cues that should prefer a non-default retriever filter.
_VISUAL_HINTS = (
    "image",
    "photo",
    "screenshot",
    "picture",
    "diagram",
    "figure",
    "scan of",
    "ocr",
)
_PDF_HINTS = ("pdf", "slideshow", "slide deck")
_BINARY_HINTS = ("attachment", "binary file", "uploaded file named")


def heuristic_retriever_tags(question: str) -> list[str]:
    """Infer kind/retriever filter tags from the question (no LLM)."""
    text = (question or "").lower()
    tags: list[str] = []
    if any(h in text for h in _VISUAL_HINTS):
        tags.extend([retriever_tag(RetrieverKind.VISUAL), kind_tag(DocumentKind.IMAGE)])
    if any(h in text for h in _PDF_HINTS):
        tags.extend(
            [retriever_tag(RetrieverKind.TEXT_HYBRID), kind_tag(DocumentKind.PDF)]
        )
    if any(h in text for h in _BINARY_HINTS) and not tags:
        tags.append(retriever_tag(RetrieverKind.BINARY_META))
    # Unique preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out
