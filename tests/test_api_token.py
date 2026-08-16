"""API token auth middleware tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.obsidian import ObsidianClient


@pytest.fixture
def authed_client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str]]:
    """Boot the app with JARVIS_API_TOKEN set; stub Obsidian."""
    token = "test-token-ws4"
    monkeypatch.setenv("JARVIS_API_TOKEN", token)
    # Explicit false — delenv alone can still pick up a lab true from `.env`.
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED_API", "false")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.resolved_api_token() == token
    assert settings.allow_unauthenticated_api is False
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client, token

    get_settings.cache_clear()


@pytest.fixture
def fail_closed_client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Boot with neither API token nor the unauthenticated lab flag."""
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED_API", "false")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.resolved_api_token() is None
    assert settings.allow_unauthenticated_api is False
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()


def test_health_open_without_token(authed_client: tuple[TestClient, str]) -> None:
    client, _token = authed_client
    response = client.get("/api/health")
    assert response.status_code == 200


def test_fail_closed_without_token_or_lab_flag(fail_closed_client: TestClient) -> None:
    """Protected routes 401 when no token is configured and lab mode is off."""
    assert fail_closed_client.get("/api/health").status_code == 200
    blocked = fail_closed_client.get("/api/profile")
    assert blocked.status_code == 401
    detail = blocked.json()["detail"] or ""
    assert "JARVIS_API_TOKEN" in detail
    assert fail_closed_client.get("/api/metrics").status_code == 401


def test_security_headers_present(fail_closed_client: TestClient) -> None:
    """Baseline security headers are attached even on health responses."""
    response = fail_closed_client.get("/api/health")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("Referrer-Policy") == "no-referrer"

def test_metrics_requires_token(authed_client: tuple[TestClient, str]) -> None:
    client, token = authed_client
    assert client.get("/api/metrics").status_code == 401
    ok = client.get("/api/metrics", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_get_profile_requires_token(authed_client: tuple[TestClient, str]) -> None:
    client, token = authed_client
    assert client.get("/api/profile").status_code == 401
    ok = client.get("/api/profile", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_get_notes_requires_token(authed_client: tuple[TestClient, str]) -> None:
    client, token = authed_client
    assert client.get("/api/notes", params={"path": "x.md"}).status_code == 401
    # Auth succeeds; vault may still be missing -> not 401.
    response = client.get(
        "/api/notes",
        params={"path": "x.md"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code != 401


def test_events_requires_token(authed_client: tuple[TestClient, str]) -> None:
    client, _token = authed_client
    assert client.get("/api/events").status_code == 401
    assert (
        client.get(
            "/api/events",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )


def test_chat_requires_token(authed_client: tuple[TestClient, str]) -> None:
    client, token = authed_client
    body = {
        "message": "hi",
        "history": [],
        "profile": {},
    }
    assert client.post("/api/chat", json=body).status_code == 401
    assert (
        client.post(
            "/api/chat",
            json=body,
            headers={"X-Jarvis-Token": "wrong"},
        ).status_code
        == 401
    )
    response = client.post(
        "/api/chat",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code != 401


def test_approvals_requires_token(authed_client: tuple[TestClient, str]) -> None:
    client, token = authed_client
    payload = {"request_id": "x", "tool": "vault_write", "approved": False}
    assert client.post("/api/approvals", json=payload).status_code == 401
    response = client.post(
        "/api/approvals",
        json=payload,
        headers={"X-Jarvis-Token": token},
    )
    # Unknown approval -> 404 after auth succeeds
    assert response.status_code == 404
