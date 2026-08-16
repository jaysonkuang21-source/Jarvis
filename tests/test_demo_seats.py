"""Tests for anonymous demo seat leases (max concurrent users per IP)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.demo_seats import reset_demo_seat_registry_for_tests
from app.obsidian import ObsidianClient


@pytest.fixture
def open_demo_client(
    tmp_settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Boot demo mode without a process API token (public anonymous clients)."""
    monkeypatch.setenv("JARVIS_DEMO_MODE", "true")
    monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
    monkeypatch.setenv("JARVIS_DEMO_MAX_SEATS_PER_IP", "4")
    get_settings.cache_clear()
    reset_demo_seat_registry_for_tests()
    settings = get_settings()
    assert settings.demo_mode is True
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()
    reset_demo_seat_registry_for_tests()


def test_demo_seat_claim_and_profile(open_demo_client: TestClient) -> None:
    """Anonymous clients claim a seat then call protected routes."""
    claim = open_demo_client.post("/api/demo/seat")
    assert claim.status_code == 200
    seat_id = claim.json()["seat_id"]
    assert seat_id

    denied = open_demo_client.get("/api/profile")
    assert denied.status_code == 429

    ok = open_demo_client.get(
        "/api/profile", headers={"X-Jarvis-Demo-Seat": seat_id}
    )
    assert ok.status_code == 200


def test_demo_seat_limit_four_per_ip(open_demo_client: TestClient) -> None:
    """A fifth distinct seat from the same IP is rejected."""
    seats = [
        open_demo_client.post("/api/demo/seat").json()["seat_id"] for _ in range(4)
    ]
    assert len(set(seats)) == 4
    fifth = open_demo_client.post("/api/demo/seat")
    assert fifth.status_code == 429

    # Existing seats can still refresh.
    refresh = open_demo_client.post(
        "/api/demo/seat", headers={"X-Jarvis-Demo-Seat": seats[0]}
    )
    assert refresh.status_code == 200
    assert refresh.json()["seat_id"] == seats[0]
