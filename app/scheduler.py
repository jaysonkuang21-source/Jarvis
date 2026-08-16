"""Durable scheduler for timers and delayed sends.

SQLite-backed on purpose. Windows sleep does not fire in-memory timers, and
neither does a closed app, so jobs live on disk and anything whose fire time
passed while the process was down is dispatched at startup rather than lost.

Gmail has no scheduled-send API -- the button in its web UI is internal-only --
so a delayed email is stored here and sent by us at the target time.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.monitoring import logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    fire_at     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    fired_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs (status, fire_at);
"""


class JobKind(StrEnum):
    TIMER = "timer"
    REMINDER = "reminder"
    EMAIL = "email"


class JobStatus(StrEnum):
    PENDING = "pending"
    FIRED = "fired"
    CANCELLED = "cancelled"


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    kind: JobKind = JobKind.TIMER
    title: str = ""
    body: str = ""
    fire_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    fired_at: datetime | None = None
    status: JobStatus = JobStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    # True when dispatched late because the app was closed or asleep.
    missed: bool = False


class CreateJobRequest(BaseModel):
    kind: JobKind = JobKind.TIMER
    title: str
    body: str = ""
    fire_at: datetime | None = None
    seconds_from_now: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    def resolve_fire_at(self) -> datetime:
        """Normalize ``fire_at`` or ``seconds_from_now`` to a UTC datetime."""
        if self.fire_at is not None:
            when = self.fire_at
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.astimezone(timezone.utc)
        if self.seconds_from_now is not None:
            return datetime.now(timezone.utc) + timedelta(seconds=self.seconds_from_now)
        msg = "Provide either fire_at or seconds_from_now"
        raise ValueError(msg)


def _row_to_job(row: sqlite3.Row) -> Job:
    """Hydrate a :class:`Job` from a SQLite row (ISO timestamps, JSON payload)."""
    return Job(
        id=row["id"],
        kind=JobKind(row["kind"]),
        title=row["title"],
        body=row["body"],
        fire_at=datetime.fromisoformat(row["fire_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        fired_at=datetime.fromisoformat(row["fired_at"]) if row["fired_at"] else None,
        status=JobStatus(row["status"]),
        payload=json.loads(row["payload"]),
    )


class JobStore:
    def __init__(self, db_path: Path) -> None:
        """Open (or create) the SQLite DB and apply the jobs schema."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()
        self._lock = asyncio.Lock()

    async def add(self, job: Job) -> Job:
        """Persist a new job; callers must already have set ``fire_at``."""
        async with self._lock:
            self._connection.execute(
                "INSERT INTO jobs (id, kind, title, body, fire_at, created_at, status, payload)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id, job.kind.value, job.title, job.body,
                    job.fire_at.isoformat(), job.created_at.isoformat(),
                    job.status.value, json.dumps(job.payload),
                ),
            )
            self._connection.commit()
        return job

    async def due(self, now: datetime) -> list[Job]:
        """Pending jobs whose ``fire_at`` is at or before ``now``."""
        async with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM jobs WHERE status = ? AND fire_at <= ? ORDER BY fire_at",
                (JobStatus.PENDING.value, now.isoformat()),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    async def pending(self) -> list[Job]:
        """All pending jobs in fire-time order, including future ones."""
        async with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY fire_at",
                (JobStatus.PENDING.value,),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    async def mark(self, job_id: str, status: JobStatus) -> None:
        """Update status and stamp ``fired_at`` (also used for cancel)."""
        async with self._lock:
            self._connection.execute(
                "UPDATE jobs SET status = ?, fired_at = ? WHERE id = ?",
                (status.value, datetime.now(timezone.utc).isoformat(), job_id),
            )
            self._connection.commit()

    def close(self) -> None:
        """Release the underlying SQLite connection."""
        self._connection.close()


Handler = Callable[[Job], Awaitable[None]]


class Scheduler:
    """Polls once a second. Precision beyond that is not worth a wakeup budget."""

    def __init__(self, store: JobStore, tick_seconds: float = 1.0) -> None:
        """Bind a store and poll interval; call :meth:`start` to begin dispatch."""
        self.store = store
        self._tick = tick_seconds
        self._handlers: list[Handler] = []
        self._task: asyncio.Task[None] | None = None

    def subscribe(self, handler: Handler) -> None:
        """Register an async callback invoked for each fired job."""
        self._handlers.append(handler)

    async def start(self) -> None:
        """Dispatch overdue jobs, then spawn the background poll loop."""
        await self._catch_up()
        self._task = asyncio.create_task(self._run(), name="jarvis-scheduler")

    async def stop(self) -> None:
        """Cancel the poll task and wait for it to unwind."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def schedule(self, request: CreateJobRequest) -> Job:
        """Create and persist a job from an API request body."""
        job = Job(
            kind=request.kind,
            title=request.title,
            body=request.body,
            fire_at=request.resolve_fire_at(),
            payload=request.payload,
        )
        return await self.store.add(job)

    async def cancel(self, job_id: str) -> None:
        """Mark a pending job cancelled so the loop will not fire it."""
        await self.store.mark(job_id, JobStatus.CANCELLED)

    async def _catch_up(self) -> None:
        """Fire anything whose time passed while we were not running."""
        overdue = await self.store.due(datetime.now(timezone.utc))
        if not overdue:
            return
        logger.info("Dispatching %d job(s) missed while offline", len(overdue))
        for job in overdue:
            job.missed = True
            await self._dispatch(job)

    async def _run(self) -> None:
        """Poll for due jobs until the task is cancelled."""
        while True:
            try:
                await asyncio.sleep(self._tick)
                for job in await self.store.due(datetime.now(timezone.utc)):
                    await self._dispatch(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad job must not kill the loop
                logger.exception("Scheduler tick failed")

    async def _dispatch(self, job: Job) -> None:
        """Mark the job fired, then fan out to handlers (errors are isolated)."""
        await self.store.mark(job.id, JobStatus.FIRED)
        for handler in self._handlers:
            try:
                await handler(job)
            except Exception:  # noqa: BLE001
                logger.exception("Job handler failed for %s", job.id)
