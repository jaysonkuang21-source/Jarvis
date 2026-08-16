"""Timer tool helpers used by the voice agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.scheduler import JobKind, JobStore, Scheduler
from app.tools import timers as timer_tools


@pytest.fixture
def scheduler(tmp_path: Path) -> Scheduler:
    """In-memory SQLite scheduler for tool tests."""
    store = JobStore(tmp_path / "jobs.db")
    sched = Scheduler(store)
    timer_tools.set_runtime_scheduler(sched)
    yield sched
    timer_tools.set_runtime_scheduler(None)
    store.close()


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("could you set a timer for me 30 seconds from now", 30),
        ("set a timer for one minute", 60),
        ("remind me in 5 minutes", 300),
        ("timer for 90 seconds", 90),
        ("timer for 30s", 30),
        ("wake me in half an hour", 1800),
        ("start a timer in a minute", 60),
        ("countdown for two minutes", 120),
        ("set a timer for a minute", 60),
        ("what is the weather", None),
        # Must not treat English "as" / "am" as 1 second / 1 minute.
        ("set this as a 30 second timer", 30),
        ("remind me when I am ready in 5 minutes", 300),
    ],
)
def test_parse_spoken_delay_seconds(utterance: str, expected: int | None) -> None:
    """Spoken timer phrases must map to exact second delays."""
    assert timer_tools.parse_spoken_delay_seconds(utterance) == expected


def test_format_delay_label() -> None:
    """Tool confirmations should use singular/plural units correctly."""
    assert timer_tools.format_delay_label(1) == "1 second"
    assert timer_tools.format_delay_label(30) == "30 seconds"
    assert timer_tools.format_delay_label(60) == "1 minute"
    assert timer_tools.format_delay_label(300) == "5 minutes"


@pytest.mark.asyncio
async def test_create_timer_tool_schedules_job(scheduler: Scheduler) -> None:
    """timer_create should persist a pending job when policy allows it."""
    result = await timer_tools.create_timer_tool(
        title="Tea",
        seconds_from_now=60,
        body="steep",
    )
    assert "Tea" in result
    assert "1 minute" in result
    pending = await scheduler.store.pending()
    assert len(pending) == 1
    assert pending[0].title == "Tea"
    assert pending[0].kind is JobKind.TIMER


@pytest.mark.asyncio
async def test_list_and_cancel_timer_tools(scheduler: Scheduler) -> None:
    """list + cancel should round-trip through short ids."""
    await timer_tools.create_timer_tool("Alpha", 120)
    listed = await timer_tools.list_timers_tool()
    assert "Alpha" in listed
    pending = await scheduler.store.pending()
    short = pending[0].id[:8]
    cancelled = await timer_tools.cancel_timer_tool(short)
    assert "Cancelled" in cancelled
    assert await scheduler.store.pending() == []


@pytest.mark.asyncio
async def test_create_timer_tool_without_scheduler() -> None:
    """Missing runtime scheduler should surface a clear error string."""
    timer_tools.set_runtime_scheduler(None)
    with pytest.raises(RuntimeError, match="not running"):
        await timer_tools.create_timer_tool("Nope", 30)
