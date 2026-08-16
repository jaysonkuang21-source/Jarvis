"""Clean and normalize note text while tracking original offsets."""

from __future__ import annotations

import re
import unicodedata

# Keep newline and tab; replace other C0 controls.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return cleaned text and per-char original (start, end) spans.

    Steps: strip BOM, normalize newlines, replace C0 controls with spaces,
    Unicode NFKC, collapse 3+ blank lines to two. Each working character maps
    back to an exclusive-end span in the input so citations stay on-disk.
    """
    if not text:
        return "", []

    start = 1 if text.startswith("\ufeff") else 0
    intermediate: list[str] = []
    spans: list[tuple[int, int]] = []

    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                intermediate.append("\n")
                spans.append((i, i + 2))
                i += 2
                continue
            intermediate.append("\n")
            spans.append((i, i + 1))
            i += 1
            continue
        if ch == "\n" or ch == "\t":
            intermediate.append(ch)
            spans.append((i, i + 1))
            i += 1
            continue
        if _CONTROL_RE.match(ch):
            intermediate.append(" ")
            spans.append((i, i + 1))
            i += 1
            continue
        intermediate.append(ch)
        spans.append((i, i + 1))
        i += 1

    # NFKC may expand or contract characters; remap each output char.
    nfkc_chars: list[str] = []
    nfkc_spans: list[tuple[int, int]] = []
    for ch, span in zip(intermediate, spans, strict=True):
        normalized = unicodedata.normalize("NFKC", ch)
        if not normalized:
            continue
        for out_ch in normalized:
            nfkc_chars.append(out_ch)
            nfkc_spans.append(span)

    joined = "".join(nfkc_chars)
    # Collapse runs of 3+ newlines to exactly two, preserving outer spans.
    out_chars: list[str] = []
    out_spans: list[tuple[int, int]] = []
    pos = 0
    for match in _BLANK_RUN_RE.finditer(joined):
        for j in range(pos, match.start()):
            out_chars.append(joined[j])
            out_spans.append(nfkc_spans[j])
        # Keep two newlines; map them to the first and last orig chars of the run.
        run_start = nfkc_spans[match.start()][0]
        run_end = nfkc_spans[match.end() - 1][1]
        out_chars.extend(["\n", "\n"])
        out_spans.append((run_start, run_end))
        out_spans.append((run_start, run_end))
        pos = match.end()
    for j in range(pos, len(joined)):
        out_chars.append(joined[j])
        out_spans.append(nfkc_spans[j])

    return "".join(out_chars), out_spans


def map_span(
    spans: list[tuple[int, int]], working_start: int, working_end: int
) -> tuple[int, int]:
    """Map a working-text half-open span to the covering original span."""
    if not spans or working_start >= working_end:
        return 0, 0
    working_start = max(0, min(working_start, len(spans)))
    working_end = max(working_start, min(working_end, len(spans)))
    if working_start >= working_end:
        if working_start > 0:
            return spans[working_start - 1]
        return spans[0] if spans else (0, 0)
    chunk = spans[working_start:working_end]
    return min(s[0] for s in chunk), max(s[1] for s in chunk)
