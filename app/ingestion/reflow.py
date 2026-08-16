"""Reflow layout-broken extract text into readable paragraphs.

PDF (and OCR) extractors often emit one newline per visual line. That hurts
chunk readability and embeddings. This module joins soft wraps while keeping
blank-line paragraph breaks.
"""

from __future__ import annotations

import re

_MULTI_SPACE_RE = re.compile(r"[ \t\u00a0]{2,}")
_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")


def reflow_extracted_text(text: str) -> str:
    """Join soft line wraps into spaces; keep blank lines as paragraph breaks.

    Also collapses runs of spaces/tabs and repairs simple end-of-line hyphenation
    (``exam-\\nple`` → ``example``). Safe for plain extracted prose, not for
    markdown that relies on hard newlines (code fences, lists).
    """
    if not text or not text.strip():
        return ""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = _PARA_SPLIT_RE.split(normalized)
    cleaned: list[str] = []
    for para in paragraphs:
        lines = [line.strip() for line in para.split("\n") if line.strip()]
        if not lines:
            continue
        joined = _join_soft_wrapped_lines(lines)
        joined = _MULTI_SPACE_RE.sub(" ", joined).strip()
        if joined:
            cleaned.append(joined)
    return "\n\n".join(cleaned)


def _join_soft_wrapped_lines(lines: list[str]) -> str:
    """Merge visual lines into one paragraph string."""
    if not lines:
        return ""
    result = lines[0]
    for nxt in lines[1:]:
        if result.endswith("-") and nxt[:1].islower():
            result = f"{result[:-1]}{nxt}"
        else:
            result = f"{result} {nxt}"
    return result
