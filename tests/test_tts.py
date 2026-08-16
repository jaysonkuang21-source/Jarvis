"""Fish Speech TTS client tests (httpx mocked; no Fish server required)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings, get_settings
from app.tts import (
    aiter_synthesize_pcm,
    fish_reachable,
    iter_synthesize_pcm,
    status,
    strip_for_speech,
    synthesize_wav,
    warm_voice,
)


@pytest.fixture
def tts_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Point TTS settings at a fake Fish Speech URL under temp data."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("JARVIS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JARVIS_TTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TTS_BASE_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("JARVIS_TTS_PROBE_TIMEOUT_SECONDS", "0.5")
    get_settings.cache_clear()
    monkeypatch.setattr("app.tts._http", None)
    settings = get_settings()
    yield settings
    get_settings.cache_clear()
    monkeypatch.setattr("app.tts._http", None)


def test_strip_for_speech_removes_code_and_links() -> None:
    """Fenced code and markdown links should not be spoken verbatim."""
    raw = "Hello **world** see [docs](https://x.test) and:\n```\nsecret\n```\ndone"
    out = strip_for_speech(raw)
    assert "secret" not in out
    assert "https" not in out
    assert "Hello" in out
    assert "world" in out
    assert "docs" in out
    assert "done" in out


def test_status_reports_engine(tts_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Status should advertise Fish Speech even when the server is down."""
    monkeypatch.setattr("app.tts.fish_reachable", lambda: False)
    st = status()
    assert st.enabled is True
    assert st.engine == "fish-speech"
    assert st.model == "openaudio-s1-mini"
    assert st.ready is False
    assert "8080" in st.base_url or "18080" in st.base_url


def test_warm_voice_is_best_effort(tts_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Warm probe must not raise when Fish is down."""
    monkeypatch.setattr("app.tts.fish_reachable", lambda: False)
    warm_voice()


def test_synthesize_disabled(tts_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled TTS must fail closed before calling Fish."""
    monkeypatch.setenv("JARVIS_TTS_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="disabled"):
        synthesize_wav("Hello")


def test_synthesize_empty_raises(tts_settings: Settings) -> None:
    """Markup-only input becomes empty and is rejected."""
    with pytest.raises(ValueError, match="Nothing to speak"):
        synthesize_wav("```\nonly code\n```")


def test_synthesize_wav_posts_to_fish(
    tts_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-streaming client should POST JSON and return the audio body."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Fake Fish ``/v1/tts`` that returns WAV-ish bytes."""
        import json

        assert request.url.path.endswith("/v1/tts")
        payload = json.loads(request.content)
        assert "Hello Jarvis" in payload["text"]
        assert payload.get("streaming") is False
        assert payload.get("seed") == 42
        assert payload.get("temperature") == 0.55
        assert payload.get("latency") == "balanced"
        assert payload.get("chunk_length") == 100
        return httpx.Response(200, content=b"RIFF....WAVE")

    transport = httpx.MockTransport(handler)

    class FakeClient(httpx.Client):
        """httpx.Client that always uses the mock transport."""

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.tts.httpx.Client", FakeClient)
    audio = synthesize_wav("Hello Jarvis")
    assert audio.startswith(b"RIFF")


def test_iter_synthesize_pcm_streams(
    tts_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streaming client should request streaming=true and yield PCM chunks."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Fake Fish streaming body as two PCM chunks."""
        import json

        payload = json.loads(request.content)
        assert payload.get("streaming") is True
        assert payload.get("format") == "wav"
        return httpx.Response(200, content=b"\x01\x00\x02\x00\x03\x00\x04\x00")

    transport = httpx.MockTransport(handler)

    class FakeClient(httpx.Client):
        """httpx.Client that always uses the mock transport."""

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.tts.httpx.Client", FakeClient)
    monkeypatch.setattr("app.tts._http", None)
    chunks = list(iter_synthesize_pcm("Hello stream"))
    assert b"".join(chunks) == b"\x01\x00\x02\x00\x03\x00\x04\x00"


@pytest.mark.asyncio
async def test_aiter_synthesize_pcm_streams(
    tts_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Async streaming client should yield the same PCM bytes."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Fake Fish streaming body for AsyncClient."""
        import json

        payload = json.loads(request.content)
        assert payload.get("streaming") is True
        return httpx.Response(200, content=b"\x10\x00\x20\x00")

    transport = httpx.MockTransport(handler)

    class FakeAsyncClient(httpx.AsyncClient):
        """httpx.AsyncClient that always uses the mock transport."""

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.tts.httpx.AsyncClient", FakeAsyncClient)
    chunks: list[bytes] = []
    async for chunk in aiter_synthesize_pcm("Hello async"):
        chunks.append(chunk)
    assert b"".join(chunks) == b"\x10\x00\x20\x00"


def test_fish_reachable_true_on_health(
    tts_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 from ``/v1/health`` counts as reachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve a cheap health page for the probe."""
        if request.url.path.rstrip("/").endswith("health"):
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    class FakeClient(httpx.Client):
        """httpx.Client that always uses the mock transport."""

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.tts.httpx.Client", FakeClient)
    assert fish_reachable() is True


def test_tts_http_endpoints(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET status and POST synthesize succeed with a stubbed Fish server."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.obsidian import ObsidianClient

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    def handler(request: httpx.Request) -> httpx.Response:
        """Fake Fish endpoints for status probe + synthesize."""
        path = request.url.path
        if path.endswith("/v1/tts") and request.method == "POST":
            return httpx.Response(200, content=b"\x01\x00\x02\x00\x03\x00\x04\x00")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)

    class FakeClient(httpx.Client):
        """httpx.Client that always uses the mock transport."""

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    class FakeAsyncClient(httpx.AsyncClient):
        """httpx.AsyncClient that always uses the mock transport."""

        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ObsidianClient, "available", available)
    monkeypatch.setattr("app.tts.httpx.Client", FakeClient)
    monkeypatch.setattr("app.tts.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.tts._http", None)
    monkeypatch.setenv("JARVIS_TTS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TTS_BASE_URL", "http://127.0.0.1:18080")
    get_settings.cache_clear()
    live = get_settings()
    monkeypatch.setattr("app.main.get_settings", lambda: live)

    with TestClient(app) as client:
        status_resp = client.get("/api/tts")
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["engine"] == "fish-speech"
        assert body["model"] == "openaudio-s1-mini"
        assert body["enabled"] is True

        health = client.get("/api/health")
        assert health.status_code == 200
        assert "fish_tts" in health.json()["checks"]

        synth = client.post("/api/tts", json={"text": "Ready."})
        assert synth.status_code == 200
        assert synth.headers.get("x-jarvis-audio-encoding") == "pcm_s16le"
        assert synth.headers.get("x-jarvis-audio-sample-rate") == str(
            live.tts_sample_rate
        )
        assert synth.content == b"\x01\x00\x02\x00\x03\x00\x04\x00"

        bad = client.post("/api/tts", json={"text": "```\nx\n```"})
        assert bad.status_code == 400
