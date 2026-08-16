"""Remove indexed documents and optional vault files."""

from __future__ import annotations

from pathlib import Path

from app.cache import clear_answer_caches
from app.db import repo
from app.ingestion.prepare import parse_frontmatter
from app.monitoring import logger
from app.security import PolicyDenied, PolicyEngine


class RemoveDocumentError(ValueError):
    """User-facing removal failure."""


def _vault_note_path(policy: PolicyEngine, vault_relative: str) -> Path:
    vault = policy.vault_path
    if vault is None:
        raise RemoveDocumentError(
            "No vault configured. Set vault_path under Settings → Rules."
        )
    rel = vault_relative.strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise RemoveDocumentError("Document path is required.")
    return (vault / rel).resolve()


def _source_file_from_note(note_path: Path) -> str | None:
    if not note_path.is_file():
        return None
    try:
        text = note_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, _, _ = parse_frontmatter(text)
    raw = meta.get("source_file")
    if raw is None:
        return None
    rel = str(raw).strip().lstrip("/")
    return rel or None


def remove_indexed_document(
    policy: PolicyEngine,
    *,
    path: str,
    delete_vault_files: bool = False,
) -> dict[str, object]:
    """Drop a document from Postgres and optionally trash vault files.

    Returns ``{removed_from_index, vault_files_trashed}``.
    """
    rel = path.strip().replace("\\", "/").lstrip("/")
    removed = repo.delete_document_by_path(rel)
    trashed: list[str] = []

    if delete_vault_files:
        note = _vault_note_path(policy, rel)
        source_rel = _source_file_from_note(note) if note.is_file() else None
        try:
            policy.operator_trash_vault_file(note)
            trashed.append(rel)
        except PolicyDenied as exc:
            raise RemoveDocumentError(str(exc)) from exc
        except FileNotFoundError:
            logger.info("Vault note already absent for %s", rel)

        if source_rel:
            try:
                source = _vault_note_path(policy, source_rel)
                policy.operator_trash_vault_file(source)
                trashed.append(source_rel)
            except PolicyDenied as exc:
                raise RemoveDocumentError(str(exc)) from exc
            except FileNotFoundError:
                logger.info("Vault source file already absent for %s", source_rel)

    if removed:
        clear_answer_caches()
    return {"removed_from_index": removed, "vault_files_trashed": trashed}


def prune_documents_missing_from_vault(vault: Path, live_paths: set[str]) -> int:
    """Remove Postgres rows for notes that no longer exist on disk."""
    removed = 0
    for path in repo.list_document_paths():
        if path not in live_paths:
            if repo.delete_document_by_path(path):
                removed += 1
    if removed:
        clear_answer_caches()
    return removed
