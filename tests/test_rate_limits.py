"""Multi-scope rate limiter (global / IP / per-token) HTTP tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import MultiRateLimiter
from app.obsidian import ObsidianClient


@pytest.fixture
def rate_client_factory(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., contextmanager]:
    """Build a TestClient with fresh rate-limit settings and limiter state."""

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app import main as main_mod

    @contextmanager
    def _build(
        *,
        rate_limit: str = "1000/minute",
        rate_limit_per_user: str = "1000/minute",
        rate_limit_global: str = "1000/minute",
        api_token: str | None = None,
    ) -> Iterator[TestClient]:
        """Apply rate env knobs, rebuild the limiter, and yield an API client."""
        monkeypatch.setenv("JARVIS_RATE_LIMIT", rate_limit)
        monkeypatch.setenv("JARVIS_RATE_LIMIT_PER_USER", rate_limit_per_user)
        monkeypatch.setenv("JARVIS_RATE_LIMIT_GLOBAL", rate_limit_global)
        if api_token is None:
            monkeypatch.delenv("JARVIS_API_TOKEN", raising=False)
            monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED_API", "true")
        else:
            monkeypatch.setenv("JARVIS_API_TOKEN", api_token)
            monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED_API", "false")
        get_settings.cache_clear()
        settings = get_settings()
        monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
        monkeypatch.setattr(main_mod, "_rate_limiter", main_mod._build_rate_limiter())
        with TestClient(main_mod.app) as client:
            yield client

    return _build


def test_settings_rate_limit_field_defaults() -> None:
    fields = Settings.model_fields
    assert fields["rate_limit"].default == "60/minute"
    assert fields["rate_limit_per_user"].default == "120/minute"
    assert fields["rate_limit_global"].default == "300/minute"


def test_ip_rate_limit_trips(rate_client_factory: Callable[..., contextmanager]) -> None:
    with rate_client_factory(rate_limit="2/minute") as client:
        assert client.get("/api/profile").status_code == 200
        assert client.get("/api/profile").status_code == 200
        blocked = client.get("/api/profile")
        assert blocked.status_code == 429
        body = blocked.json()
        assert body["error"] == "rate_limited"
        assert "ip" in body["detail"]
        # Metrics is not exempt.
        metrics = client.get("/api/metrics")
        assert metrics.status_code == 429


def test_global_rate_limit_trips(
    rate_client_factory: Callable[..., contextmanager],
) -> None:
    with rate_client_factory(rate_limit_global="2/minute") as client:
        assert client.get("/api/profile").status_code == 200
        assert client.get("/api/profile").status_code == 200
        blocked = client.get("/api/profile")
        assert blocked.status_code == 429
        assert blocked.json()["error"] == "rate_limited"
        assert "global" in blocked.json()["detail"]


def test_per_token_rate_limit_trips(
    rate_client_factory: Callable[..., contextmanager],
) -> None:
    token = "test-session-token"
    headers = {"Authorization": f"Bearer {token}"}
    with rate_client_factory(
        rate_limit_per_user="2/minute", api_token=token
    ) as client:
        assert client.get("/api/profile", headers=headers).status_code == 200
        assert client.get("/api/profile", headers=headers).status_code == 200
        blocked = client.get("/api/profile", headers=headers)
        assert blocked.status_code == 429
        assert blocked.json()["error"] == "rate_limited"
        assert "user" in blocked.json()["detail"]


def test_forged_bearer_skips_user_bucket(
    rate_client_factory: Callable[..., contextmanager],
) -> None:
    """Invalid Bearer burns IP/global only; no separate user bucket."""
    forged = {"Authorization": "Bearer forged-not-the-configured-token"}
    with rate_client_factory(
        rate_limit="1000/minute",
        rate_limit_per_user="1/minute",
        rate_limit_global="1000/minute",
        api_token="real-token",
    ) as client:
        # Forged token fails auth; hits must not open a user bucket.
        for _ in range(3):
            assert client.get("/api/profile", headers=forged).status_code == 401
        ok = client.get(
            "/api/profile",
            headers={"Authorization": "Bearer real-token"},
        )
        assert ok.status_code == 200
        # One validated user hit consumed; second should trip user.
        blocked = client.get(
            "/api/profile",
            headers={"Authorization": "Bearer real-token"},
        )
        assert blocked.status_code == 429
        assert "user" in blocked.json()["detail"]


def test_forged_bearer_without_token_mode_skips_user_bucket(
    rate_client_factory: Callable[..., contextmanager],
) -> None:
    """When token mode is off, a Bearer header still must not open a user bucket."""
    headers = {"Authorization": "Bearer arbitrary"}
    with rate_client_factory(rate_limit_per_user="1/minute") as client:
        assert client.get("/api/profile", headers=headers).status_code == 200
        # Would be 429 on "user" if forged tokens got their own bucket.
        second = client.get("/api/profile", headers=headers)
        assert second.status_code == 200


def test_rate_limit_logs_warning(
    rate_client_factory: Callable[..., contextmanager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 denials emit a warning with scope/method/path (no secrets)."""
    from app import main as main_mod

    warnings: list[tuple] = []

    def capture(msg: str, *args: object, **_kwargs: object) -> None:
        """Collect warning format args for assertion."""
        warnings.append((msg, args))

    monkeypatch.setattr(main_mod.logger, "warning", capture)
    with rate_client_factory(rate_limit="1/minute") as client:
        assert client.get("/api/profile").status_code == 200
        blocked = client.get("/api/profile")
        assert blocked.status_code == 429
    assert warnings
    msg, args = warnings[-1]
    assert msg == "Rate limited (%s) %s %s"
    assert args == ("ip", "GET", "/api/profile")


def test_health_exempt_from_rate_limits(
    rate_client_factory: Callable[..., contextmanager],
) -> None:
    with rate_client_factory(rate_limit="1/minute") as client:
        assert client.get("/api/profile").status_code == 200
        assert client.get("/api/profile").status_code == 429
        health = client.get("/api/health")
        assert health.status_code == 200
        # Exhausted budgets still rate-limit metrics.
        assert client.get("/api/metrics").status_code == 429


def test_loopback_client_exempt_from_rate_limits(
    rate_client_factory: Callable[..., contextmanager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Desktop / Vite peers on loopback must never trip IP buckets."""
    from app import main as main_mod

    monkeypatch.setattr(main_mod, "_is_loopback_client", lambda _request: True)
    with rate_client_factory(rate_limit="1/minute") as client:
        assert client.get("/api/profile").status_code == 200
        # Second call would be 429 without the loopback exemption.
        assert client.get("/api/profile").status_code == 200
        assert client.get("/api/metrics").status_code == 200


def test_validated_token_skips_ip_bucket(
    rate_client_factory: Callable[..., contextmanager],
) -> None:
    """First-party Bearer traffic must not burn the shared IP budget."""
    token = "desktop-session-token"
    headers = {"Authorization": f"Bearer {token}"}
    with rate_client_factory(
        rate_limit="1/minute",
        rate_limit_per_user="1000/minute",
        rate_limit_global="1000/minute",
        api_token=token,
    ) as client:
        assert client.get("/api/profile", headers=headers).status_code == 200
        # IP budget is 1/minute; token peers skip IP and succeed again.
        assert client.get("/api/profile", headers=headers).status_code == 200


def test_multi_rate_limiter_evicts_idle_keys() -> None:
    """Cap on ``_hits`` drops oldest non-global keys when over budget."""
    limiter = MultiRateLimiter(
        ip_max=100,
        ip_window=60.0,
        user_max=100,
        user_window=60.0,
        global_max=1000,
        global_window=60.0,
        max_keys=3,
    )
    limiter._hits = {
        "global": [1.0],
        "ip:a": [2.0],
        "ip:b": [3.0],
        "ip:c": [4.0],
        "ip:d": [5.0],
    }
    limiter._evict_idle()
    assert "global" in limiter._hits
    assert len(limiter._hits) <= 3
    assert "ip:a" not in limiter._hits
