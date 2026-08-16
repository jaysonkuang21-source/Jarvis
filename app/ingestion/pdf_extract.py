"""Extract plain text from PDF bytes via pypdf."""

from __future__ import annotations

import io

from app.ingestion.reflow import reflow_extracted_text
from app.monitoring import logger

# Soft cap so a huge PDF cannot flood the companion note / embed batch.
_MAX_CHARS = 200_000


def extract_pdf_text(data: bytes) -> str:
    """Return concatenated page text from a PDF, or empty string on failure.

    Page text is reflowed so visual line wraps become spaces and blank lines
    stay as paragraph breaks (better chunk visibility).
    """
    if not data:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf is not installed; PDF text extraction skipped")
        return ""

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        parts: list[str] = []
        total = 0
        for index, page in enumerate(reader.pages):
            try:
                chunk = (page.extract_text() or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.info("PDF page %s extract failed (%s)", index + 1, exc)
                continue
            if not chunk:
                continue
            chunk = reflow_extracted_text(chunk)
            if not chunk:
                continue
            parts.append(chunk)
            total += len(chunk)
            if total >= _MAX_CHARS:
                parts.append("\n\n_[truncated: PDF text exceeded size cap]_")
                break
        # Page boundaries stay as paragraph breaks after per-page reflow.
        return reflow_extracted_text("\n\n".join(parts))
    except Exception as exc:  # noqa: BLE001
        logger.info("PDF extract failed (%s)", exc)
        return ""
