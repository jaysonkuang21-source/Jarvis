"""Tests for extracted-text reflow (PDF/OCR soft-wrap cleanup)."""

from __future__ import annotations

from app.ingestion.reflow import reflow_extracted_text


def test_reflow_joins_soft_wrapped_lines() -> None:
    """Single newlines within a paragraph become spaces."""
    raw = "my\nipad.\nYet\nno\nmatter\nwhat\nthey\ntried,"
    assert reflow_extracted_text(raw) == "my ipad. Yet no matter what they tried,"


def test_reflow_keeps_paragraph_breaks() -> None:
    """Blank lines remain paragraph separators."""
    raw = "First paragraph\ncontinues here.\n\nSecond paragraph\nalso continues."
    assert (
        reflow_extracted_text(raw)
        == "First paragraph continues here.\n\nSecond paragraph also continues."
    )


def test_reflow_collapses_extra_spaces() -> None:
    """Runs of spaces from glyph gaps are collapsed."""
    raw = "my  ipad.  Yet  no  matter"
    assert reflow_extracted_text(raw) == "my ipad. Yet no matter"


def test_reflow_repairs_line_end_hyphenation() -> None:
    """Common PDF hyphenation at line end is rejoined."""
    raw = "inexplica-\nble hospitality"
    assert reflow_extracted_text(raw) == "inexplicable hospitality"


def test_reflow_empty() -> None:
    """Blank input stays empty."""
    assert reflow_extracted_text("") == ""
    assert reflow_extracted_text("   \n\n  ") == ""
