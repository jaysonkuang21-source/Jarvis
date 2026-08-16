"""Write user-supplied documents into the vault Inbox for digestion."""

from __future__ import annotations

import base64
from pathlib import Path

from app.ingestion.formats import (
    DocumentKind,
    FormatSpec,
    RetrieverKind,
    kind_tag,
    resolve_format,
    retriever_tag,
)
from app.ingestion.docx_extract import extract_docx_text
from app.ingestion.ocr import ocr_image, tesseract_available
from app.ingestion.pdf_extract import extract_pdf_text
from app.monitoring import logger
from app.security import PolicyDenied, PolicyEngine, sanitize_filename

INBOX_FOLDER = "Inbox"
FILES_FOLDER = "files"
MAX_FILE_BYTES = 10_000_000


class InboxError(ValueError):
    """User-facing ingest failure (bad name, empty body, size, etc.)."""


def _unique_path(directory: Path, filename: str) -> Path:
    """Pick a non-colliding path under ``directory`` for ``filename``."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 10_000):
        alt = directory / f"{stem}-{index}{suffix}"
        if not alt.exists():
            return alt
    msg = f"Could not find a free name for {filename}"
    raise InboxError(msg)


def prepare_note_filename(name: str, *, default_stem: str = "note") -> str:
    """Sanitize a filename and force a markdown suffix for the searchable note."""
    safe = sanitize_filename(name or default_stem, fallback=default_stem)
    path = Path(safe)
    stem = path.stem or default_stem
    return f"{stem}.md"


def prepare_binary_filename(name: str, *, default_stem: str = "file") -> str:
    """Sanitize a binary filename while keeping its original suffix."""
    safe = sanitize_filename(name or default_stem, fallback=default_stem)
    path = Path(safe)
    stem = path.stem or default_stem
    suffix = path.suffix.lower() if path.suffix else ".bin"
    return f"{stem}{suffix}"


def _frontmatter(*, title: str, tags: list[str], extra: dict[str, str] | None = None) -> str:
    """Build a minimal YAML frontmatter block."""
    lines = ["---", f"title: {title!r}"]
    if tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f"  - {tag}")
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value!r}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _assert_writable(policy: PolicyEngine, target: Path) -> Path:
    """Resolve ``target`` and enforce operator vault-write sandbox."""
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved = target.resolve()
    try:
        policy.assert_operator_vault_write(resolved)
    except PolicyDenied as exc:
        raise InboxError(str(exc)) from exc
    return resolved


def write_inbox_note(
    policy: PolicyEngine,
    *,
    filename: str,
    content: str,
    tags: list[str] | None = None,
) -> str:
    """Write markdown into ``Inbox/`` under the vault; return vault-relative path."""
    vault = policy.vault_path
    if vault is None:
        raise InboxError("No vault configured. Set vault path under Settings → Rules.")

    text = content if isinstance(content, str) else str(content)
    if not text.strip():
        raise InboxError("Document content is empty.")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise InboxError(f"Document exceeds {MAX_FILE_BYTES // 1_000_000} MB size limit.")

    fmt = resolve_format(filename if Path(filename).suffix else f"{filename}.md")
    note_tags = list(tags or [])
    for tag in (kind_tag(fmt.kind), retriever_tag(fmt.retriever)):
        if tag not in note_tags:
            note_tags.append(tag)

    if not text.lstrip().startswith("---"):
        title = Path(filename).stem or "note"
        text = _frontmatter(title=title, tags=note_tags) + text

    safe_name = prepare_note_filename(filename)
    inbox = vault / INBOX_FOLDER
    resolved = _assert_writable(policy, _unique_path(inbox, safe_name))
    resolved.write_text(text, encoding="utf-8", newline="\n")
    return resolved.relative_to(vault.resolve()).as_posix()


def _build_image_note(
    *,
    title: str,
    rel_file: str,
    ocr_text: str,
    tags: list[str],
) -> str:
    """Markdown companion for an uploaded image (OCR body when available)."""
    body = ocr_text.strip() or "_No OCR text extracted. Install Tesseract for image text._"
    return (
        _frontmatter(title=title, tags=tags, extra={"source_file": rel_file})
        + f"# {title}\n\n"
        + f"![[{rel_file}]]\n\n"
        + "## Extracted text\n\n"
        + body
        + "\n"
    )


def _build_pdf_note(
    *,
    title: str,
    rel_file: str,
    extracted: str,
    tags: list[str],
) -> str:
    """Markdown companion for an uploaded PDF with extracted page text."""
    body = extracted.strip() or (
        "_No extractable text found (scanned PDF?). "
        "Original file is kept under Inbox/files/._"
    )
    return (
        _frontmatter(
            title=title,
            tags=tags,
            extra={"source_file": rel_file, "mime": "application/pdf"},
        )
        + f"# {title}\n\n"
        + f"Source: `/{rel_file}`\n\n"
        + "## Extracted text\n\n"
        + body
        + "\n"
    )


def _build_docx_note(
    *,
    title: str,
    rel_file: str,
    extracted: str,
    tags: list[str],
) -> str:
    """Markdown companion for an uploaded DOCX with extracted body text."""
    body = extracted.strip() or (
        "_No extractable text found in this DOCX. "
        "Original file is kept under Inbox/files/._"
    )
    return (
        _frontmatter(
            title=title,
            tags=tags,
            extra={
                "source_file": rel_file,
                "mime": (
                    "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"
                ),
            },
        )
        + f"# {title}\n\n"
        + f"Source: `/{rel_file}`\n\n"
        + "## Extracted text\n\n"
        + body
        + "\n"
    )


def _build_binary_note(
    *,
    title: str,
    rel_file: str,
    fmt: FormatSpec,
    tags: list[str],
) -> str:
    """Metadata note for PDFs / opaque binaries until a dedicated extractor lands."""
    return (
        _frontmatter(
            title=title,
            tags=tags,
            extra={
                "source_file": rel_file,
                "mime": fmt.mime,
                "retriever": fmt.retriever.value,
            },
        )
        + f"# {title}\n\n"
        + f"- Source file: `/{rel_file}`\n"
        + f"- Kind: `{fmt.kind.value}`\n"
        + f"- Preferred retriever: `{fmt.retriever.value}`\n"
        + f"- MIME: `{fmt.mime}`\n\n"
        + f"{fmt.note}\n"
    )


def ingest_upload_bytes(
    policy: PolicyEngine,
    *,
    filename: str,
    data: bytes,
    mime: str | None = None,
) -> dict[str, str | list[str]]:
    """Store an arbitrary upload and produce a searchable Inbox note.

    Returns ``{note_path, file_path?, kind, retriever, tags}``.
    """
    vault = policy.vault_path
    if vault is None:
        raise InboxError("No vault configured. Set vault path under Settings → Rules.")
    if not data:
        raise InboxError(f"{filename} is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise InboxError(
            f"{filename} exceeds {MAX_FILE_BYTES // 1_000_000} MB size limit."
        )

    fmt = resolve_format(filename, mime=mime)
    tags = [kind_tag(fmt.kind), retriever_tag(fmt.retriever)]
    title = Path(sanitize_filename(filename, fallback="file")).stem or "file"

    if fmt.kind is DocumentKind.TEXT:
        text = data.decode("utf-8", errors="replace")
        note_path = write_inbox_note(
            policy, filename=filename, content=text, tags=tags
        )
        return {
            "note_path": note_path,
            "kind": fmt.kind.value,
            "retriever": fmt.retriever.value,
            "tags": tags,
        }

    # Keep original bytes under Inbox/files/, then write a companion .md note.
    files_dir = vault / INBOX_FOLDER / FILES_FOLDER
    bin_name = prepare_binary_filename(filename)
    bin_resolved = _assert_writable(policy, _unique_path(files_dir, bin_name))
    bin_resolved.write_bytes(data)
    rel_file = bin_resolved.relative_to(vault.resolve()).as_posix()

    if fmt.kind is DocumentKind.IMAGE:
        ocr_text = ""
        if tesseract_available():
            try:
                ocr_text = ocr_image(bin_resolved) or ""
            except Exception as exc:  # noqa: BLE001
                logger.info("OCR failed for %s (%s)", rel_file, exc)
        else:
            logger.info("OCR skipped for %s: tesseract not on PATH", rel_file)
        note_body = _build_image_note(
            title=title, rel_file=rel_file, ocr_text=ocr_text, tags=tags
        )
    elif fmt.kind is DocumentKind.PDF:
        extracted = extract_pdf_text(data)
        note_body = _build_pdf_note(
            title=title, rel_file=rel_file, extracted=extracted, tags=tags
        )
    elif fmt.kind is DocumentKind.DOCX:
        extracted = extract_docx_text(data)
        note_body = _build_docx_note(
            title=title, rel_file=rel_file, extracted=extracted, tags=tags
        )
    else:
        note_body = _build_binary_note(
            title=title, rel_file=rel_file, fmt=fmt, tags=tags
        )

    note_path = write_inbox_note(
        policy, filename=f"{title}.md", content=note_body, tags=tags
    )
    return {
        "note_path": note_path,
        "file_path": rel_file,
        "kind": fmt.kind.value,
        "retriever": fmt.retriever.value,
        "tags": tags,
    }


def decode_data_url_or_base64(raw: str) -> bytes:
    """Decode a browser FileReader data URL or bare base64 payload."""
    text = (raw or "").strip()
    if not text:
        raise InboxError("Upload payload is empty.")
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise InboxError("Could not decode base64 upload.") from exc


def decode_upload_bytes(data: bytes, filename: str) -> str:
    """Decode uploaded bytes as UTF-8 text (with replacement for odd encodings)."""
    if len(data) > MAX_FILE_BYTES:
        raise InboxError(
            f"{filename} exceeds {MAX_FILE_BYTES // 1_000_000} MB size limit."
        )
    if not data.strip():
        raise InboxError(f"{filename} is empty.")
    return data.decode("utf-8", errors="replace")
