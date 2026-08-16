"""Normalize / OCR / prepare note tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingestion.normalize import map_span, normalize_text
from app.ingestion.ocr import find_image_embeds, format_ocr_block
from app.ingestion.prepare import parse_frontmatter, prepare_note


def test_normalize_strips_bom_and_newlines() -> None:
    raw = "\ufeffhello\r\n\r\n\r\nworld\x00done"
    cleaned, spans = normalize_text(raw)
    assert cleaned.startswith("hello\n\nworld")
    assert "\x00" not in cleaned
    assert len(spans) == len(cleaned)
    # Entire cleaned string maps into the original.
    o_start, o_end = map_span(spans, 0, len(cleaned))
    assert 0 <= o_start < o_end <= len(raw)


def test_normalize_collapses_blank_runs() -> None:
    raw = "a\n\n\n\n\nb"
    cleaned, spans = normalize_text(raw)
    assert cleaned == "a\n\nb"
    assert map_span(spans, 0, 1) == (0, 1)


def test_find_image_embeds_wiki_and_markdown() -> None:
    text = "See ![[shot.png]] and ![alt](img/photo.jpg) later."
    embeds = find_image_embeds(text)
    assert len(embeds) == 2
    assert embeds[0].target == "shot.png"
    assert embeds[1].target == "img/photo.jpg"


def test_prepare_note_parses_tags_and_maps_offsets(tmp_path: Path) -> None:
    vault = tmp_path
    note = vault / "Note.md"
    raw = "---\ntitle: Hello\ntags: [a, b]\n---\n\n# Body\n\nPlain text.\n"
    note.write_text(raw, encoding="utf-8")
    prepared = prepare_note(vault, note, raw)
    assert prepared.tags == ["a", "b"]
    assert prepared.title == "Hello"
    assert "Body" in prepared.working
    assert len(prepared.spans) == len(prepared.working)
    # Map a slice of working body back into the original.
    idx = prepared.working.find("Plain text")
    assert idx >= 0
    o_start, o_end = prepared.map_span(idx, idx + len("Plain text"))
    assert raw[o_start:o_end] == "Plain text"


def test_prepare_ocr_replacement_maps_to_image_syntax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path
    note = vault / "Note.md"
    image = vault / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    raw = "Intro\n\n![[shot.png]]\n\nOutro\n"
    note.write_text(raw, encoding="utf-8")

    monkeypatch.setattr(
        "app.ingestion.prepare.tesseract_available", lambda: True
    )
    monkeypatch.setattr(
        "app.ingestion.prepare.ocr_image",
        lambda _path: "READ TEXT FROM IMAGE",
    )

    prepared = prepare_note(vault, note, raw)
    assert "READ TEXT FROM IMAGE" in prepared.working
    assert "[OCR:shot.png]" in prepared.working
    # Citation for OCR block should land on the original embed syntax.
    start = prepared.working.find(format_ocr_block("shot.png", "READ TEXT FROM IMAGE"))
    end = start + len(format_ocr_block("shot.png", "READ TEXT FROM IMAGE"))
    o_start, o_end = prepared.map_span(start, end)
    assert raw[o_start:o_end] == "![[shot.png]]"


def test_parse_frontmatter_list_tags() -> None:
    text = "---\ntags:\n  - one\n  - two\n---\nBody\n"
    meta, body, offset = parse_frontmatter(text)
    assert meta["tags"] == ["one", "two"]
    assert body.startswith("Body")
    assert offset > 0
