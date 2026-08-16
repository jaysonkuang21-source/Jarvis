"""OCR embedded images in markdown notes via system tesseract."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.monitoring import logger
from app.security import is_within

# Obsidian embed: ![[path|size]] or ![[path]]
_WIKI_EMBED_RE = re.compile(
    r"!\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]"
)
# Standard markdown image: ![alt](path)
_MD_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)"
)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp"}


@dataclass(frozen=True)
class ImageEmbed:
    """One image embed in note text ready for optional OCR."""

    start: int
    end: int
    raw: str
    target: str


def find_image_embeds(text: str) -> list[ImageEmbed]:
    """Locate Obsidian and markdown image embeds in document order."""
    found: list[ImageEmbed] = []
    for match in _WIKI_EMBED_RE.finditer(text):
        found.append(
            ImageEmbed(
                start=match.start(),
                end=match.end(),
                raw=match.group(0),
                target=match.group(1).strip(),
            )
        )
    for match in _MD_IMAGE_RE.finditer(text):
        found.append(
            ImageEmbed(
                start=match.start(),
                end=match.end(),
                raw=match.group(0),
                target=match.group(1).strip().strip("<>"),
            )
        )
    found.sort(key=lambda e: e.start)
    return found


def resolve_image_path(vault: Path, note_path: Path, target: str) -> Path | None:
    """Resolve an image target relative to the note or vault root."""
    cleaned = target.replace("\\", "/").lstrip("/")
    if not cleaned:
        return None
    candidates = [
        note_path.parent / cleaned,
        vault / cleaned,
    ]
    # Obsidian often stores images without an extension in wikilinks.
    stem_candidates: list[Path] = []
    for base in candidates:
        stem_candidates.append(base)
        if not base.suffix:
            for suffix in _IMAGE_SUFFIXES:
                stem_candidates.append(base.with_suffix(suffix))

    for path in stem_candidates:
        try:
            resolved = path.resolve()
            vault_resolved = vault.resolve()
            if not is_within(resolved, vault_resolved):
                continue
            if resolved.is_file() and resolved.suffix.lower() in _IMAGE_SUFFIXES:
                return resolved
        except OSError:
            continue
    return None


def tesseract_available() -> bool:
    """True when the tesseract binary is on PATH."""
    return shutil.which("tesseract") is not None


def ocr_image(path: Path) -> str | None:
    """Run tesseract on ``path``; return text or None on failure/skip."""
    if not tesseract_available():
        return None
    try:
        completed = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "eng"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("OCR skipped for %s (%s)", path, exc)
        return None
    if completed.returncode != 0:
        logger.info(
            "OCR failed for %s: %s",
            path,
            (completed.stderr or "").strip()[:200],
        )
        return None
    text = (completed.stdout or "").strip()
    if not text:
        return None
    from app.ingestion.reflow import reflow_extracted_text

    cleaned = reflow_extracted_text(text)
    return cleaned or None


def format_ocr_block(image_name: str, text: str) -> str:
    """Wrap OCR output so chunkers can treat it as plain prose."""
    return f"\n[OCR:{image_name}]\n{text}\n[/OCR]\n"
