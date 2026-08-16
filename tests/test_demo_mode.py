"""Demo-mode lockdown and Supabase auth helper tests."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.demo import (
    DEMO_CHAT_MODEL,
    force_demo_profile,
    locked_profile_field_changes,
    scrub_absolute_path,
)
from app.models import Profile, Provider
from app.obsidian import ObsidianClient


@pytest.fixture
def demo_client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Boot the app with demo mode and a shared API token."""
    token = "demo-test-token"
    monkeypatch.setenv("JARVIS_DEMO_MODE", "true")
    monkeypatch.setenv("JARVIS_API_TOKEN", token)
    monkeypatch.setenv("JARVIS_ALLOW_UNAUTHENTICATED_API", "false")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.demo_mode is True
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


def test_force_demo_profile_locks_openai_mini() -> None:
    """Demo profile pin forces GPT-4o mini on OpenAI for chat and embeds."""
    profile = force_demo_profile(Profile())
    assert profile.chat_model == DEMO_CHAT_MODEL
    assert profile.chat_provider is Provider.OPENAI
    assert profile.embedding_provider is Provider.OPENAI
    assert profile.rag_mode.value == "regular"


def test_locked_profile_field_changes_detects_model_edits() -> None:
    """Model field diffs are listed for 403 decisions."""
    before = force_demo_profile(Profile()).model_dump(mode="json")
    after = {**before, "chat_model": "gpt-4o"}
    assert locked_profile_field_changes(before, after) == ["chat_model"]


def test_scrub_absolute_path_keeps_basename_only() -> None:
    """API scrubbing must not return home-directory prefixes."""
    assert scrub_absolute_path(r"D:\Personal Projects\Jarvis\demo\vault") == "vault"
    assert scrub_absolute_path(None) is None


def test_demo_health_omits_local_plugin_fields(demo_client: TestClient) -> None:
    """Health in demo mode exposes only safe readiness flags."""
    # Health is always open; drop the auth header for this call.
    response = demo_client.get(
        "/api/health", headers={"Authorization": ""}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "demo"
    assert body["checks"]["demo_mode"] is True
    assert "obsidian_plugin" not in body["checks"]
    assert body["checks"]["chat_model"] == "gpt-4o-mini"


def test_demo_system_scrubs_hardware(demo_client: TestClient) -> None:
    """System probe must not leak host RAM/GPU in demo mode."""
    response = demo_client.get("/api/system")
    assert response.status_code == 200
    body = response.json()
    assert body["ram_total_mb"] is None
    assert body["gpus"] == []
    assert any("demo_mode" in err for err in body["probe_errors"])


def test_demo_rejects_model_profile_put(demo_client: TestClient) -> None:
    """PUT /api/profile rejects locked model fields in demo mode."""
    current = demo_client.get("/api/profile").json()
    current["chat_model"] = "gpt-4o"
    response = demo_client.put("/api/profile", json=current)
    assert response.status_code == 403
    assert "cannot edit" in response.json()["error"].lower() or "locked" in response.json()["error"].lower()


def test_demo_allows_ingest_profile_put(demo_client: TestClient) -> None:
    """Demo may change ingest_effort while models stay locked."""
    current = demo_client.get("/api/profile").json()
    current["ingest_effort"] = "low"
    response = demo_client.put("/api/profile", json=current)
    assert response.status_code == 200
    assert response.json()["ingest_effort"] == "low"
    assert response.json()["chat_model"] == DEMO_CHAT_MODEL


def test_demo_rejects_timers(demo_client: TestClient) -> None:
    """Timer CRUD is disabled in demo mode."""
    assert demo_client.get("/api/timers").status_code == 403
    assert (
        demo_client.post(
            "/api/timers",
            json={"kind": "timer", "title": "x", "seconds_from_now": 60},
        ).status_code
        == 403
    )


def test_demo_rejects_voice(demo_client: TestClient) -> None:
    """Voice endpoint is disabled in demo mode."""
    profile = demo_client.get("/api/profile").json()
    response = demo_client.post(
        "/api/voice",
        json={"message": "hi", "history": [], "profile": profile},
    )
    assert response.status_code == 403


def test_demo_operator_reindex_allowed(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Demo reindex is allowed for seated clients (shared sample + Inbox index)."""
    seat = demo_client.post(
        "/api/demo/seat", headers={"Authorization": ""}
    ).json()["seat_id"]
    # Without DATABASE_URL the demo_client uses placeholder — expect 400 not 403.
    response = demo_client.post(
        "/api/index/reindex",
        json={},
        headers={"Authorization": "", "X-Jarvis-Demo-Seat": seat},
    )
    assert response.status_code in (200, 400, 409)
    assert response.status_code != 403


def test_demo_chat_requires_session_llm_key(demo_client: TestClient) -> None:
    """Demo chat without X-Jarvis-User-LLM-Key returns 400."""
    profile = demo_client.get("/api/profile").json()
    response = demo_client.post(
        "/api/chat",
        json={"message": "hello", "history": [], "profile": profile},
    )
    assert response.status_code == 400
    assert "API key" in response.json()["error"]


def test_demo_chat_binds_request_llm_key(
    demo_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Demo chat binds BYOK into context around engine.query."""
    from app.agent import PlaceholderRetrievalEngine
    from app.llm_session import get_request_llm_api_key
    from app.models import DoneEvent, TokenEvent

    async def fake_query(self, message, profile, history=None):  # noqa: ANN001
        """Emit the bound request key so the test can assert BYOK wiring."""
        key = get_request_llm_api_key() or ""
        yield TokenEvent(text=f"bound:{key}")
        yield DoneEvent(message_id="test")

    monkeypatch.setattr(PlaceholderRetrievalEngine, "query", fake_query)

    profile = demo_client.get("/api/profile").json()
    response = demo_client.post(
        "/api/chat",
        headers={"X-Jarvis-User-LLM-Key": "sk-user-secret"},
        json={"message": "hello", "history": [], "profile": profile},
    )
    assert response.status_code == 200
    assert "bound:sk-user-secret" in response.text


def test_demo_chunks_list_allowed_with_seat(demo_client: TestClient) -> None:
    """Chunk inspector API is available in demo when a seat is held."""
    # Operator token on demo_client skips seat requirement.
    response = demo_client.get("/api/index/documents/chunks?path=Inbox/x.md")
    # No DB → 400; with DB would be 200. Never 403 in demo anymore.
    assert response.status_code in (200, 400)
    assert response.status_code != 403


def test_assert_hosted_demo_posture_requires_demo() -> None:
    """Production non-loopback without demo_mode raises."""
    from app.main import assert_hosted_demo_posture

    settings = Settings(
        app_env="production",
        allow_non_loopback=True,
        demo_mode=False,
    )
    with pytest.raises(RuntimeError, match="DEMO_MODE"):
        assert_hosted_demo_posture(settings)


def test_parse_user_llm_headers() -> None:
    """BYOK header helper reads key and optional base URL."""
    from app.llm_session import parse_user_llm_headers

    class H(dict):
        """Minimal header map with dict.get semantics."""

        def get(self, key, default=None):  # noqa: ANN001
            return super().get(key, default)

    key, url = parse_user_llm_headers(
        H(
            {
                "X-Jarvis-User-LLM-Key": " sk-abc ",
                "X-Jarvis-User-LLM-Base-Url": "https://openrouter.ai/api/v1/",
            }
        )
    )
    assert key == "sk-abc"
    assert url == "https://openrouter.ai/api/v1"


def test_build_chat_model_demo_requires_request_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In demo mode, build_chat_model refuses the process env OpenAI key."""
    monkeypatch.setenv("JARVIS_DEMO_MODE", "true")
    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", "sk-env-only")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.demo_mode is True
    monkeypatch.setattr("app.agent.get_settings", lambda: settings)

    from app.agent import build_chat_model
    from app.llm_session import request_llm_credentials
    from app.models import Provider

    with pytest.raises(RuntimeError, match="session OpenAI-compatible"):
        build_chat_model("gpt-4o-mini", Provider.OPENAI)

    with request_llm_credentials("sk-session", "https://example.com/v1"):
        model = build_chat_model("gpt-4o-mini", Provider.OPENAI)
        assert model is not None

    get_settings.cache_clear()


def test_demo_rejects_rules_put(demo_client: TestClient) -> None:
    """Rules writes are forbidden in demo mode."""
    policy = demo_client.get("/api/rules").json()
    response = demo_client.put(
        "/api/rules",
        json={"policy": policy, "confirm_elevation": False},
    )
    assert response.status_code == 403


def test_demo_rejects_model_recommend(demo_client: TestClient) -> None:
    """Model recommendations are disabled in demo mode."""
    response = demo_client.post(
        "/api/models/recommend",
        json={"apply": False, "top_n": 3, "online": False},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_verify_supabase_access_token_success() -> None:
    """Valid Auth /user responses become a SupabaseUser and are cached."""
    from app.auth_supabase import (
        clear_supabase_auth_cache,
        verify_supabase_access_token,
    )
    from app.config import Settings

    clear_supabase_auth_cache()
    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-test",
    )

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json = lambda: {
        "id": "user-123",
        "email": "judge@example.com",
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.auth_supabase.httpx.AsyncClient", return_value=mock_client):
        user = await verify_supabase_access_token("access-token", settings=settings)
    assert user is not None
    assert user.id == "user-123"
    assert user.email == "judge@example.com"

    # Second call should hit cache (no extra HTTP).
    with patch("app.auth_supabase.httpx.AsyncClient") as client_cls:
        again = await verify_supabase_access_token("access-token", settings=settings)
        client_cls.assert_not_called()
    assert again is not None
    assert again.id == "user-123"
    clear_supabase_auth_cache()


@pytest.mark.asyncio
async def test_verify_supabase_access_token_rejects_401() -> None:
    """Non-200 Auth responses fail closed."""
    from app.auth_supabase import (
        clear_supabase_auth_cache,
        verify_supabase_access_token,
    )
    from app.config import Settings

    clear_supabase_auth_cache()
    settings = Settings(
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-test",
    )
    mock_response = AsyncMock()
    mock_response.status_code = 401
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.auth_supabase.httpx.AsyncClient", return_value=mock_client):
        user = await verify_supabase_access_token("bad", settings=settings)
    assert user is None
    clear_supabase_auth_cache()
