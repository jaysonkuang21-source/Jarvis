"""FastAPI route smoke tests with the placeholder retrieval engine."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import Profile
from app.obsidian import ObsidianClient


@pytest.fixture
def client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Boot the app against temp settings; stub Obsidian probes."""

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert "version" in body
    assert "checks" in body
    # Obsidian is stubbed offline — still reported, but must not gate overall ok.
    assert body["checks"]["obsidian_plugin"] is False
    assert body["checks"]["postgres_configured"] is False
    assert body["checks"]["postgres_ready"] is False
    # Core readiness follows vault (+ optional required Postgres), not Obsidian.
    if body["checks"]["vault_configured"]:
        assert body["ok"] is True
        assert body["status"] == "healthy"
    else:
        assert body["ok"] is False
        assert body["status"] == "degraded"


def test_metrics(client: TestClient) -> None:
    response = client.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert "total_requests" in body
    assert "error_rate" in body


def test_profile_get_and_validate(client: TestClient) -> None:
    get_resp = client.get("/api/profile")
    assert get_resp.status_code == 200
    profile = get_resp.json()
    assert profile["id"] == "default"

    validate = client.post("/api/profile/validate", json=Profile().model_dump(mode="json"))
    assert validate.status_code == 200
    assert "valid" in validate.json()


def test_index_status_placeholder(client: TestClient) -> None:
    response = client.get("/api/index/status")
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "placeholder"
    assert body["ready"] is False


def test_timers_list(client: TestClient) -> None:
    response = client.get("/api/timers")
    assert response.status_code == 200
    assert response.json() == []
