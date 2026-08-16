"""Voice agent stream and stuck-reindex recovery helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from app.models import DoneEvent, Profile, TokenEvent


@pytest.mark.asyncio
async def test_stream_voice_direct_tokens(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conversational voice should stream tokens immediately (no ainvoke wait)."""
    from app import voice as voice_mod

    class FakeModel:
        """Minimal chat model stand-in."""

        def bind_tools(self, _tools: list[Any]) -> FakeModel:
            return self

        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            raise AssertionError("conversational voice must not ainvoke")

        async def astream(self, _messages: list[Any]):
            yield AIMessage(content="Hello from voice.")

    monkeypatch.setattr(voice_mod, "build_chat_model", lambda *a, **k: FakeModel())

    events = [event async for event in voice_mod.stream_voice("hi", profile, [])]
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert any("Hello from voice" in e.text for e in tokens)
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_stream_voice_uses_voice_model_not_chat(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voice must call build_chat_model with voice_model, never chat_model."""
    from app import voice as voice_mod
    from app.models import Provider

    profile = profile.model_copy(
        update={
            "chat_model": "qwen3.5:9b",
            "chat_provider": Provider.OLLAMA,
            "voice_model": "qwen3.5:2b",
            "voice_provider": Provider.OLLAMA,
        }
    )
    captured: dict[str, Any] = {}

    class FakeModel:
        def bind_tools(self, _tools: list[Any]) -> FakeModel:
            return self

        async def astream(self, _messages: list[Any]):
            yield AIMessage(content="ok")

    def fake_build(model: str, provider: Provider, **kwargs: Any) -> FakeModel:
        captured["model"] = model
        captured["provider"] = provider
        captured["kwargs"] = kwargs
        return FakeModel()

    monkeypatch.setattr(voice_mod, "build_chat_model", fake_build)

    _ = [event async for event in voice_mod.stream_voice("hi there", profile, [])]
    assert captured["model"] == "qwen3.5:2b"
    assert captured["model"] != profile.chat_model
    assert captured["provider"] is Provider.OLLAMA
    assert captured["kwargs"].get("reasoning") is False
    assert "keep_alive" in captured["kwargs"]
    assert captured["kwargs"].get("max_context_tokens", 9999) <= 4096


def test_resolve_voice_model_falls_back_when_blank() -> None:
    """Blank voice_model should fall back to the small default, not chat_model."""
    from app.voice import resolve_voice_model
    from app.models import Provider

    profile = Profile(
        chat_model="qwen3.5:9b",
        voice_model="  ",
        voice_provider=Provider.OLLAMA,
    )
    model, provider = resolve_voice_model(profile)
    assert model == "qwen3.5:2b"
    assert provider is Provider.OLLAMA
    assert model != profile.chat_model


@pytest.mark.asyncio
async def test_stream_voice_strips_think_tags(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaked think traces must never reach the voice token stream."""
    from app import voice as voice_mod

    class FakeModel:
        def bind_tools(self, _tools: list[Any]) -> FakeModel:
            return self

        async def astream(self, _messages: list[Any]):
            yield AIMessage(
                content="Hello! </think> How are things going?"
            )

    monkeypatch.setattr(voice_mod, "build_chat_model", lambda *a, **k: FakeModel())

    events = [
        event async for event in voice_mod.stream_voice("hello hello", profile, [])
    ]
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    joined = "".join(e.text for e in tokens)
    assert "think" not in joined.lower()
    assert "Hello!" in joined
    assert "How are things going?" in joined


@pytest.mark.asyncio
async def test_stream_voice_can_call_vault_search(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the utterance needs notes, run vault_search then answer."""
    from app import voice as voice_mod
    from app.models import ToolCallEvent, ToolResultEvent

    class FakeModel:
        def __init__(self) -> None:
            self.phase = 0

        def bind_tools(self, _tools: list[Any]) -> FakeModel:
            return self

        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "vault_search",
                        "args": {"query": "project notes"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )

        async def astream(self, _messages: list[Any]):
            yield AIMessage(content="Found two notes about projects.")

    async def fake_search(query: str, _profile: Profile) -> str:
        assert "project" in query
        return "Vault excerpts:\n- Note A: hello"

    monkeypatch.setattr(voice_mod, "build_chat_model", lambda *a, **k: FakeModel())
    monkeypatch.setattr(voice_mod, "_search_vault", fake_search)

    events = [
        event
        async for event in voice_mod.stream_voice("what are my projects?", profile, [])
    ]
    assert any(isinstance(e, ToolCallEvent) and e.name == "vault_search" for e in events)
    assert any(isinstance(e, ToolResultEvent) and e.ok for e in events)
    assert any(
        isinstance(e, TokenEvent) and "Found two notes" in e.text for e in events
    )


@pytest.mark.asyncio
async def test_stream_voice_can_create_timer(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timer phrases must bind tools and call timer_create (not the fast path)."""
    from app import voice as voice_mod
    from app.models import ToolCallEvent, ToolResultEvent

    class FakeModel:
        def bind_tools(self, tools: list[Any]) -> FakeModel:
            names = {getattr(t, "name", "") for t in tools}
            assert "timer_create" in names
            return self

        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "timer_create",
                        "args": {
                            "title": "Tea",
                            "seconds_from_now": 60,
                            "body": "",
                        },
                        "id": "call-timer",
                        "type": "tool_call",
                    }
                ],
            )

        async def astream(self, _messages: list[Any]):
            yield AIMessage(content="Your tea timer is set for one minute.")

    async def fake_create(title: str, seconds_from_now: int, body: str = "") -> str:
        assert title == "Tea"
        assert seconds_from_now == 60
        return "Timer 'Tea' set for 1 minute (id abcdef12)."

    monkeypatch.setattr(voice_mod, "build_chat_model", lambda *a, **k: FakeModel())
    monkeypatch.setattr(
        "app.tools.timers.create_timer_tool", fake_create
    )

    events = [
        event
        async for event in voice_mod.stream_voice(
            "start a timer for one minute called Tea", profile, []
        )
    ]
    assert any(isinstance(e, ToolCallEvent) and e.name == "timer_create" for e in events)
    assert any(isinstance(e, ToolResultEvent) and e.ok for e in events)
    assert any(
        isinstance(e, TokenEvent) and "tea timer" in e.text.lower() for e in events
    )


@pytest.mark.asyncio
async def test_stream_voice_overrides_wrong_timer_seconds(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spoken delay wins when the model copies a bad example like 300."""
    from app import voice as voice_mod
    from app.models import ToolCallEvent

    class FakeModel:
        def bind_tools(self, tools: list[Any]) -> FakeModel:
            return self

        async def ainvoke(self, _messages: list[Any]) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "timer_create",
                        "args": {
                            "title": "Quick Check-In",
                            "seconds_from_now": 300,
                            "body": "",
                        },
                        "id": "call-bad-timer",
                        "type": "tool_call",
                    }
                ],
            )

        async def astream(self, _messages: list[Any]):
            yield AIMessage(content="Timer set for 30 seconds.")

    async def fake_create(title: str, seconds_from_now: int, body: str = "") -> str:
        assert seconds_from_now == 30
        return "Timer 'Quick Check-In' set for 30 seconds (id abcdef12)."

    monkeypatch.setattr(voice_mod, "build_chat_model", lambda *a, **k: FakeModel())
    monkeypatch.setattr("app.tools.timers.create_timer_tool", fake_create)

    events = [
        event
        async for event in voice_mod.stream_voice(
            "could you set a timer for me 30 seconds from now", profile, []
        )
    ]
    call = next(e for e in events if isinstance(e, ToolCallEvent))
    assert call.arguments["seconds_from_now"] == 30



def test_likely_needs_timer_hints() -> None:
    """Timer routing hints must catch common spoken requests."""
    from app.voice import _likely_needs_timer, _likely_needs_tools

    assert _likely_needs_timer("set a timer for five minutes")
    assert _likely_needs_tools("remind me in 10 minutes")
    assert not _likely_needs_timer("what is the weather")


def test_index_status_marks_stale_when_db_indexing_without_live_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB indexing=true with no in-process job should surface indexing_stale."""
    from app.retrieval.engine import PostgresHybridEngine

    engine = PostgresHybridEngine()
    engine._indexing = False

    monkeypatch.setattr(
        "app.retrieval.engine.database_configured", lambda: True
    )
    monkeypatch.setattr(
        "app.retrieval.engine.try_ensure_schema", lambda: True
    )
    monkeypatch.setattr(
        "app.retrieval.engine.repo.fetch_index_meta",
        lambda: {"ready": False, "indexing": True, "embedding_dims": None},
    )
    monkeypatch.setattr(
        "app.retrieval.engine.repo.measure_vector_column_dims", lambda _t: None
    )
    monkeypatch.setattr("app.retrieval.engine.repo.count_table", lambda _t: 0)
    policy = MagicMock()
    policy.vault_path = None
    monkeypatch.setattr("app.security.get_policy_engine", lambda: policy)

    status = engine._sync_index_status()
    assert status.indexing is True
    assert status.indexing_stale is True


def test_index_status_not_stale_when_process_job_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-process _indexing=True must clear indexing_stale (force reindex target)."""
    from app.retrieval.engine import PostgresHybridEngine

    engine = PostgresHybridEngine()
    engine._indexing = True

    monkeypatch.setattr(
        "app.retrieval.engine.database_configured", lambda: True
    )
    monkeypatch.setattr(
        "app.retrieval.engine.try_ensure_schema", lambda: True
    )
    monkeypatch.setattr(
        "app.retrieval.engine.repo.fetch_index_meta",
        lambda: {"ready": False, "indexing": True, "embedding_dims": None},
    )
    monkeypatch.setattr(
        "app.retrieval.engine.repo.measure_vector_column_dims", lambda _t: None
    )
    monkeypatch.setattr("app.retrieval.engine.repo.count_table", lambda _t: 0)
    policy = MagicMock()
    policy.vault_path = None
    monkeypatch.setattr("app.security.get_policy_engine", lambda: policy)

    status = engine._sync_index_status()
    assert status.indexing is True
    assert status.indexing_stale is False
