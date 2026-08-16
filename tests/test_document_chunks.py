"""Tests for GET /api/index/documents/chunks (desktop chunk inspector)."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.obsidian import ObsidianClient


@pytest.fixture
def desktop_client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Boot the app without demo mode and with a shared API token."""
    token = "desktop-test-token"
    monkeypatch.setenv("JARVIS_DEMO_MODE", "false")
    monkeypatch.setenv("JARVIS_API_TOKEN", token)
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED_API", "false")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.demo_mode is False
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app.main import app

    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {token}"})
        yield test_client

    get_settings.cache_clear()


def test_list_document_chunks_requires_database(desktop_client: TestClient) -> None:
    """Without Postgres configured, chunk listing returns 400."""
    response = desktop_client.get("/api/index/documents/chunks?path=Inbox/a.md")
    assert response.status_code == 400


def test_list_document_chunks_returns_inventory(
    desktop_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path returns total + chunk previews from the repo helper."""
    monkeypatch.setattr("app.main.database_configured", lambda: True)

    def fake_list(path: str, limit: int = 500) -> tuple[int, list[dict]]:
        """Return two synthetic chunks for the requested path."""
        assert path == "Inbox/note.md"
        assert limit >= 1
        return 2, [
            {
                "chunk_id": "c0000",
                "text": "first chunk text [[Friend]]",
                "heading_path": ["Intro"],
                "char_start": 0,
                "char_end": 16,
                "note_path": path,
                "note_title": "note",
                "tags": ["essay"],
                "wikilinks": ["Friend"],
                "entities": ["Friend"],
            },
            {
                "chunk_id": "c0001",
                "text": "second",
                "heading_path": [],
                "char_start": 17,
                "char_end": 23,
                "note_path": path,
                "note_title": "note",
                "tags": ["essay"],
                "wikilinks": [],
                "entities": [],
            },
        ]

    with patch("app.db.repo.list_chunks_for_path", fake_list):
        response = desktop_client.get(
            "/api/index/documents/chunks?path=Inbox/note.md&limit=10"
        )
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "Inbox/note.md"
    assert body["total"] == 2
    assert len(body["chunks"]) == 2
    assert body["chunks"][0]["chunk_id"] == "c0000"
    assert body["chunks"][0]["tags"] == ["essay"]
    assert body["chunks"][0]["wikilinks"] == ["Friend"]
    assert body["chunks"][0]["entities"] == ["Friend"]


def test_list_indexed_documents_requires_database(desktop_client: TestClient) -> None:
    """Without Postgres configured, document listing returns 400."""
    response = desktop_client.get("/api/index/documents")
    assert response.status_code == 400


def test_list_indexed_documents_returns_inventory(
    desktop_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path returns indexed paths with chunk counts from the repo."""
    monkeypatch.setattr("app.main.database_configured", lambda: True)

    def fake_docs() -> list[dict]:
        """Return one synthetic indexed document."""
        return [
            {
                "path": "Inbox/RAG Chunking Benchmark — Black Holes.md",
                "title": "RAG Chunking Benchmark — Black Holes",
                "tags": ["text-hybrid"],
                "chunk_count": 12,
            }
        ]

    with patch("app.db.repo.list_indexed_documents", fake_docs):
        response = desktop_client.get("/api/index/documents")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["documents"][0]["path"].endswith("Black Holes.md")
    assert body["documents"][0]["chunk_count"] == 12
    assert body["documents"][0]["tags"] == ["text-hybrid"]