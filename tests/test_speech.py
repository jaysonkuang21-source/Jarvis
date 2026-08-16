"""Speech helpers and Permissions-Policy for mic / TTS."""

from __future__ import annotations

from app.tts import strip_for_speech


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


def test_strip_for_speech_removes_think_tags() -> None:
    """Qwen-style think traces must never be spoken."""
    from app.tts import strip_for_speech, strip_think_tags

    raw = "Hello! </think> How are things going?"
    assert "think" not in strip_think_tags(raw).lower()
    spoken = strip_for_speech(
        "<think>plan the reply</think>Hello! How are things going?"
    )
    assert "plan the reply" not in spoken
    assert "Hello!" in spoken
    assert "How are things going?" in spoken


def test_permissions_policy_allows_mic_self() -> None:
    """Voice STT requires microphone=(self) while camera/geo stay blocked."""
    from app.main import _SECURITY_HEADERS

    policy = _SECURITY_HEADERS["Permissions-Policy"]
    assert "microphone=(self)" in policy
    assert "camera=()" in policy
    assert "geolocation=()" in policy
