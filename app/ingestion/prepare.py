"""Prepare vault notes: frontmatter, normalize, OCR, offset mapping."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.ingestion.normalize import map_span, normalize_text
from app.ingestion.ocr import (
    find_image_embeds,
    format_ocr_block,
    ocr_image,
    resolve_image_path,
    tesseract_available,
)
from app.ingestion.tags import normalize_tags
from app.monitoring import logger

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


@dataclass
class PreparedNote:
    """Normalized working text plus a map back to on-disk offsets."""

    original: str
    working: str
    spans: list[tuple[int, int]]
    title: str | None = None
    tags: list[str] = field(default_factory=list)
    body_offset: int = 0

    def map_span(self, working_start: int, working_end: int) -> tuple[int, int]:
        """Map a working half-open span onto the original note."""
        return map_span(self.spans, working_start, working_end)


def parse_frontmatter(text: str) -> tuple[dict[str, object], str, int]:
    """Split optional YAML frontmatter; return (meta, body, body_start)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text, 0
    meta = _parse_simple_yaml(match.group(1))
    return meta, text[match.end() :], match.end()


def _parse_simple_yaml(block: str) -> dict[str, object]:
    """Parse a minimal YAML subset used in Obsidian frontmatter (tags/title)."""
    meta: dict[str, object] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if key == "tags" and (rest == "" or rest == "|" or rest == ">"):
            tags: list[str] = []
            i += 1
            while i < len(lines):
                item = lines[i]
                stripped = item.strip()
                if stripped.startswith("- "):
                    tags.append(_strip_yaml_scalar(stripped[2:]))
                    i += 1
                    continue
                break
            meta["tags"] = tags
            continue
        if key == "tags" and rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            meta["tags"] = [
                _strip_yaml_scalar(part) for part in inner.split(",") if part.strip()
            ]
            i += 1
            continue
        if rest:
            meta[key] = _strip_yaml_scalar(rest)
        i += 1
    return meta


def _strip_yaml_scalar(value: str) -> str:
    """Remove surrounding quotes from a YAML scalar."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def extract_tags(meta: dict[str, object]) -> list[str]:
    """Normalize frontmatter tags to a unique ordered slug list."""
    raw = meta.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
        return normalize_tags(parts)
    if isinstance(raw, list):
        return normalize_tags(str(t).strip() for t in raw if str(t).strip())
    return []


def prepare_note(vault: Path, path: Path, raw_text: str) -> PreparedNote:
    """Normalize body text and inline OCR for embedded images when possible."""
    meta, body, body_start = parse_frontmatter(raw_text)
    tags = extract_tags(meta)
    title: str | None = None
    if isinstance(meta.get("title"), str) and meta["title"].strip():
        title = str(meta["title"]).strip()

    # Build working text from the full original so absolute offsets stay valid.
    # Frontmatter stays in place (normalized), body is normalized + OCR'd.
    prefix = raw_text[:body_start]
    prefix_norm, prefix_spans = normalize_text(prefix) if prefix else ("", [])
    body_norm, body_spans = normalize_text(body)

    # Shift body spans by original body_start (normalize_text used body alone).
    body_spans = [(s + body_start, e + body_start) for s, e in body_spans]

    working = prefix_norm + body_norm
    spans = prefix_spans + body_spans

    if tesseract_available():
        working, spans = _apply_ocr(vault, path, working, spans)
    else:
        embeds = find_image_embeds(working)
        if embeds:
            logger.info(
                "OCR skipped for %s: tesseract not on PATH (%s image embeds)",
                path.name,
                len(embeds),
            )

    return PreparedNote(
        original=raw_text,
        working=working,
        spans=spans,
        title=title,
        tags=tags,
        body_offset=body_start,
    )


def _apply_ocr(
    vault: Path,
    path: Path,
    working: str,
    spans: list[tuple[int, int]],
) -> tuple[str, list[tuple[int, int]]]:
    """Replace image embeds with OCR text, remapping spans to original syntax."""
    embeds = find_image_embeds(working)
    if not embeds:
        return working, spans

    # Process last-to-first so earlier offsets stay valid during rebuild.
    embeds = sorted(embeds, key=lambda e: e.start, reverse=True)
    for embed in embeds:
        image = resolve_image_path(vault, path, embed.target)
        if image is None:
            continue
        text = ocr_image(image)
        if not text:
            continue
        # Map embed syntax back to the on-disk span before rewriting working.
        orig_start, orig_end = map_span(spans, embed.start, embed.end)
        block = format_ocr_block(image.name, text)
        working = working[: embed.start] + block + working[embed.end :]
        new_spans = spans[: embed.start]
        for _ in block:
            new_spans.append((orig_start, orig_end))
        new_spans.extend(spans[embed.end :])
        spans = new_spans

    return working, spans
