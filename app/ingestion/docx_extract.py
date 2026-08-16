"""Extract plain text from .docx bytes (OOXML zip + word/document.xml)."""

from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET

from app.monitoring import logger

# Soft cap so a huge document cannot flood the companion note / embed batch.
_MAX_CHARS = 200_000

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx_text(data: bytes) -> str:
    """Return paragraph text from a .docx file, or empty string on failure."""
    if not data:
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                xml_bytes = archive.read("word/document.xml")
            except KeyError:
                logger.info("DOCX missing word/document.xml")
                return ""
        root = ET.fromstring(xml_bytes)
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        logger.info("DOCX extract failed (%s)", exc)
        return ""

    parts: list[str] = []
    total = 0
    for paragraph in root.iter(f"{_W_NS}p"):
        runs = [
            node.text
            for node in paragraph.iter(f"{_W_NS}t")
            if node.text
        ]
        if not runs:
            continue
        line = "".join(runs).strip()
        if not line:
            continue
        parts.append(line)
        total += len(line)
        if total >= _MAX_CHARS:
            parts.append("\n\n_[truncated: DOCX text exceeded size cap]_")
            break
    return "\n\n".join(parts).strip()
