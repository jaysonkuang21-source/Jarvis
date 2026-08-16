"""Voice/chat tool helpers that schedule jobs through the runtime Scheduler."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.monitoring import logger
from app.scheduler import CreateJobRequest, JobKind
from app.security import PolicyDenied, get_policy_engine

if TYPE_CHECKING:
    from app.scheduler import Scheduler

_scheduler: Scheduler | None = None

# Spoken number words used in short timer phrases ("in one minute").
# Do not include "a"/"an" here — they collide with English ("as", "am").
_NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "forty-five": 45,
    "forty five": 45,
    "sixty": 60,
    "ninety": 90,
}

# Digits may use short units (30s); number words require full unit names.
_DIGIT_AMOUNT = r"(?P<n>\d+(?:\.\d+)?)"
_WORD_AMOUNT = (
    r"(?P<wn>"
    + "|".join(re.escape(w) for w in sorted(_NUMBER_WORDS, key=len, reverse=True))
    + r")"
)
_DELAY_RE = re.compile(
    rf"\b{_DIGIT_AMOUNT}\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b"
    rf"|\b{_WORD_AMOUNT}\s*(?P<wunit>seconds?|secs?|minutes?|mins?|hours?|hrs?)\b"
    rf"|\bhalf\s+(?:an?\s+)?(?P<half>hour|minute)s?\b"
    rf"|\b(?:an?|in\s+an?)\s+(?P<a_unit>minute|second|hour)s?\b",
    re.IGNORECASE,
)


def set_runtime_scheduler(scheduler: Scheduler | None) -> None:
    """Register the process scheduler for tool calls (cleared on shutdown)."""
    global _scheduler
    _scheduler = scheduler


def get_runtime_scheduler() -> Scheduler | None:
    """Return the scheduler bound at app lifespan, if any."""
    return _scheduler


def _require_scheduler() -> Scheduler:
    """Return the live scheduler or raise a speech-friendly error."""
    if _scheduler is None:
        raise RuntimeError("Timer service is not running.")
    return _scheduler


def _amount_to_number(raw: str) -> float | None:
    """Map a digit or number-word token to a float amount."""
    text = raw.strip().lower()
    if text in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[text])
    try:
        return float(text)
    except ValueError:
        return None


def _unit_to_seconds(unit: str) -> int:
    """Convert a spoken unit token to a seconds multiplier."""
    u = unit.lower()
    if u in {"s", "sec", "secs", "second", "seconds"}:
        return 1
    if u in {"m", "min", "mins", "minute", "minutes"}:
        return 60
    return 3600


def parse_spoken_delay_seconds(text: str) -> int | None:
    """Extract a countdown delay from a spoken timer request, if unambiguous.

    Returns None when no duration is found so the model/tool args stay as-is.
    Prefers the first explicit duration phrase (e.g. \"30 seconds from now\").
    """
    if not (text or "").strip():
        return None
    match = _DELAY_RE.search(text)
    if match is None:
        return None

    if match.group("half"):
        half = match.group("half").lower()
        seconds = 30 if half.startswith("minute") else 1800
        return max(1, seconds)

    if match.group("a_unit"):
        return _unit_to_seconds(match.group("a_unit"))

    raw = match.group("n") or match.group("wn") or ""
    unit = match.group("unit") or match.group("wunit") or ""
    amount = _amount_to_number(raw)
    if amount is None or amount <= 0 or not unit:
        return None
    seconds = int(round(amount * _unit_to_seconds(unit)))
    return max(1, seconds)


def format_delay_label(seconds_from_now: int) -> str:
    """Human-readable duration for tool results and spoken confirmations."""
    if seconds_from_now < 60:
        unit = "second" if seconds_from_now == 1 else "seconds"
        return f"{seconds_from_now} {unit}"
    if seconds_from_now % 3600 == 0:
        hours = seconds_from_now // 3600
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit}"
    if seconds_from_now % 60 == 0:
        mins = seconds_from_now // 60
        unit = "minute" if mins == 1 else "minutes"
        return f"{mins} {unit}"
    mins = seconds_from_now // 60
    secs = seconds_from_now % 60
    return f"{mins} minute{'s' if mins != 1 else ''} {secs} seconds"


async def create_timer_tool(
    title: str,
    seconds_from_now: int,
    body: str = "",
) -> str:
    """Create a countdown timer after a policy allowlist check."""
    policy = get_policy_engine()
    try:
        policy.check("timer_create").raise_for_decision()
    except PolicyDenied as exc:
        return f"Cannot create timer: {exc}"

    if seconds_from_now < 1:
        return "Cannot create timer: seconds_from_now must be at least 1."

    scheduler = _require_scheduler()
    job = await scheduler.schedule(
        CreateJobRequest(
            kind=JobKind.TIMER,
            title=(title or "Timer").strip() or "Timer",
            body=(body or "").strip(),
            seconds_from_now=int(seconds_from_now),
        )
    )
    logger.info("Voice tool created timer %s fire_at=%s", job.id, job.fire_at.isoformat())
    when = format_delay_label(int(seconds_from_now))
    return (
        f"Timer '{job.title}' set for {when} "
        f"(id {job.id[:8]}, fires at {job.fire_at.isoformat()})."
    )


async def list_timers_tool() -> str:
    """List pending timers after a policy allowlist check."""
    policy = get_policy_engine()
    try:
        policy.check("timer_list").raise_for_decision()
    except PolicyDenied as exc:
        return f"Cannot list timers: {exc}"

    scheduler = _require_scheduler()
    pending = await scheduler.store.pending()
    if not pending:
        return "No timers are pending."
    lines = [
        f"- {job.title} at {job.fire_at.isoformat()} (id {job.id[:8]})"
        for job in pending[:10]
    ]
    return "Pending timers:\n" + "\n".join(lines)


async def cancel_timer_tool(job_id: str) -> str:
    """Cancel a pending timer by id (full or short prefix) after policy check."""
    policy = get_policy_engine()
    try:
        policy.check("timer_cancel").raise_for_decision()
    except PolicyDenied as exc:
        return f"Cannot cancel timer: {exc}"

    needle = (job_id or "").strip()
    if not needle:
        return "Cannot cancel timer: missing job id."

    scheduler = _require_scheduler()
    pending = await scheduler.store.pending()
    match = next(
        (
            job
            for job in pending
            if job.id == needle or job.id.startswith(needle)
        ),
        None,
    )
    if match is None:
        return f"No pending timer matches id '{needle}'."
    await scheduler.cancel(match.id)
    return f"Cancelled timer '{match.title}' (id {match.id[:8]})."
