"""Document removal from the Postgres index and vault."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import repo, try_ensure_schema
from app.ingestion.inbox import ingest_upload_bytes
from app.ingestion.remove import prune_documents_missing_from_vault, remove_indexed_document
from app.obsidian import ObsidianClient
from tests.test_inbox_ingest import _vault_engine


@pytest.fixture
def client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Boot the app against temp settings; stub Obsidian probes."""

    async def available(_self: ObsidianClient) -> bool:
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_ready() -> None:
    """Skip when Postgres is not configured for integration-style repo tests."""
    if not try_ensure_schema():
        pytest.skip("Postgres not configured")


def test_remove_indexed_document_trashes_vault_files(tmp_path: Path) -> None:
    """Operator removal drops the index row and moves note + source to trash."""
    vault = tmp_path / "vault"
    vault.mkdir()
    engine = _vault_engine(vault)
    result = ingest_upload_bytes(
        engine,
        filename="note.md",
        data=b"# Hello\n\nBody text.\n",
    )
    note_path = str(result["note_path"])
    repo.upsert_document(note_path, "note", 1.0, "abc", tags=["kind-text"])

    outcome = remove_indexed_document(
        engine, path=note_path, delete_vault_files=True
    )
    assert outcome["removed_from_index"] is True
    assert note_path in outcome["vault_files_trashed"]
    assert not (vault / note_path).exists()
    assert repo.delete_document_by_path(note_path) is False


def test_prune_documents_missing_from_vault(
    tmp_path: Path, db_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reindex prune drops Postgres rows for notes deleted from disk."""
    vault = tmp_path / "vault"
    inbox = vault / "Inbox"
    inbox.mkdir(parents=True)
    rel = "Inbox/gone-prune-test.md"
    repo.upsert_document(rel, "gone", 1.0, "deadbeef", tags=[])
    monkeypatch.setattr(repo, "list_document_paths", lambda: [rel])

    removed = prune_documents_missing_from_vault(vault, live_paths=set())
    assert removed == 1
    assert repo.delete_document_by_path(rel) is False


def test_delete_indexed_document_api(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_ready: None
) -> None:
    """DELETE /api/index/documents removes a row from Postgres."""
    vault = tmp_path / "vault"
    vault.mkdir()
    engine = _vault_engine(vault)
    monkeypatch.setattr("app.main.get_policy_engine", lambda: engine)

    ingest = ingest_upload_bytes(
        engine, filename="brief.md", data=b"# Brief\n\nShort.\n"
    )
    note_path = str(ingest["note_path"])
    repo.upsert_document(note_path, "brief", 1.0, "hash", tags=[])

    response = client.request(
        "DELETE",
        "/api/index/documents",
        json={"path": note_path, "delete_vault_files": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["removed_from_index"] is True
    assert repo.delete_document_by_path(note_path) is False
