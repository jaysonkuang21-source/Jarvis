"""Logging, in-process metrics, and LangSmith tracing.

Tracing is opt-in per profile. Enabling it uploads prompt text and retrieved
note content to LangSmith's cloud, which cuts against running models locally
for privacy, so nothing here turns itself on.
"""

from __future__ import annotations

import logging
import os
import json
import time
from contextlib import contextmanager
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

from langchain_core.tracers.context import tracing_v2_enabled
from langsmith import Client as LangSmithClient

from app.config import get_settings

# Standard LogRecord attributes we never dump into the "extra" JSON field.
_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "exc_info",
        "exc_text",
        "thread",
        "threadName",
        "taskName",
        "asctime",
    }
)


class JSONFormatter(logging.Formatter):
    """One JSON object per line so logs are greppable and machine-readable."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record to a single JSON line, including non-reserved extras."""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
        }
        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        if extra:
            payload["extra"] = extra
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str = "jarvis") -> logging.Logger:
    """Return a logger; attach a JSON stream handler if it has none yet."""
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def configure_logging(level: int = logging.INFO) -> None:
    """Install JSON logging for the jarvis logger and quiet noisy libraries."""
    log = get_logger("jarvis")
    log.setLevel(level)
    for handler in log.handlers:
        handler.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


logger = get_logger("jarvis")


class MetricsCollector:
    """In-process counters for the current process lifetime."""

    def __init__(self) -> None:
        """Initialize zeroed counters for the process lifetime."""
        self.metrics: dict[str, float | int] = {
            "total_requests": 0,
            "error_count": 0,
            "latency_sum": 0.0,
            "latency_count": 0,
            "tokens_input": 0,
            "tokens_output": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "config_changes": 0,
            "rate_limit_denials": 0,
            "rate_limit_exemptions": 0,
        }

    def record_request(
        self,
        latency_ms: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        *,
        error: bool = False,
        cache_hit: bool | None = None,
    ) -> None:
        """Accumulate latency, tokens, and optional error/cache outcome for one request."""
        self.metrics["total_requests"] += 1
        self.metrics["latency_sum"] += latency_ms
        self.metrics["latency_count"] += 1
        self.metrics["tokens_input"] += input_tokens
        self.metrics["tokens_output"] += output_tokens

        if error:
            self.metrics["error_count"] += 1

        if cache_hit is True:
            self.metrics["cache_hits"] += 1
        elif cache_hit is False:
            self.metrics["cache_misses"] += 1

    def record_config_change(self) -> None:
        """Bump the config-change counter when profile or rules are written."""
        self.metrics["config_changes"] += 1

    def record_rate_limit_denial(self, scope: str) -> None:
        """Count a 429 denial; ``scope`` is for logs only (ip / user / global)."""
        _ = scope
        self.metrics["rate_limit_denials"] += 1

    def record_rate_limit_exemption(self, reason: str) -> None:
        """Count a first-party / path exemption that skipped rate-limit buckets."""
        _ = reason
        self.metrics["rate_limit_exemptions"] += 1

    def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Accumulate token usage without counting a new HTTP request."""
        if input_tokens:
            self.metrics["tokens_input"] += int(input_tokens)
        if output_tokens:
            self.metrics["tokens_output"] += int(output_tokens)

    @property
    def summary(self) -> dict[str, Any]:
        """Derived rates and averages for logging and the metrics endpoint."""
        latency_count = int(self.metrics["latency_count"])
        total = int(self.metrics["total_requests"])
        hits = int(self.metrics["cache_hits"])
        misses = int(self.metrics["cache_misses"])
        cache_total = hits + misses

        avg_latency = (
            float(self.metrics["latency_sum"]) / latency_count if latency_count else 0.0
        )
        error_rate = (
            int(self.metrics["error_count"]) / total if total else 0.0
        )
        cache_hit_rate = hits / cache_total if cache_total else 0.0

        return {
            "total_requests": total,
            "error_count": int(self.metrics["error_count"]),
            "error_rate": round(error_rate, 4),
            "avg_latency_ms": round(avg_latency, 2),
            "tokens_input": int(self.metrics["tokens_input"]),
            "tokens_output": int(self.metrics["tokens_output"]),
            "cache_hit_rate": round(cache_hit_rate, 4),
            "config_changes": int(self.metrics["config_changes"]),
            "rate_limit_denials": int(self.metrics["rate_limit_denials"]),
            "rate_limit_exemptions": int(self.metrics["rate_limit_exemptions"]),
        }

    def to_response(self) -> Any:
        """Shape counters for :class:`~app.models.MetricsResponse`."""
        from app.models import MetricsResponse

        s = self.summary
        return MetricsResponse(
            total_requests=int(s["total_requests"]),
            total_errors=int(s["error_count"]),
            error_rate=f"{float(s['error_rate']) * 100:.2f}%",
            avg_latency_ms=float(s["avg_latency_ms"]),
            cache_hit_rate=f"{float(s['cache_hit_rate']) * 100:.2f}%",
            total_input_tokens=int(s["tokens_input"]),
            total_output_tokens=int(s["tokens_output"]),
            config_changes=int(s["config_changes"]),
            rate_limit_denials=int(s["rate_limit_denials"]),
            rate_limit_exemptions=int(s["rate_limit_exemptions"]),
        )


class RequestTimer:
    """Context manager to measure request processing time in milliseconds."""

    def __init__(self) -> None:
        """Prepare timer fields; timing starts on enter."""
        self.start_time = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> RequestTimer:
        """Start the high-resolution clock and return self."""
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        """Store elapsed wall time in ``elapsed_ms``; never suppress exceptions."""
        self.elapsed_ms = (time.perf_counter() - self.start_time) * 1000


_metrics: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    """Return the process-wide metrics collector, creating it on first use."""
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics


def log_config_change(
    kind: str,
    *,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    changed: Mapping[str, Any] | None = None,
) -> None:
    """Audit a settings write from the UI (profile or rules)."""
    get_metrics().record_config_change()
    logger.info(
        "Config changed: %s",
        kind,
        extra={
            "event": "config_change",
            "kind": kind,
            "changed": dict(changed or {}),
            "before": dict(before or {}),
            "after": dict(after or {}),
        },
    )


def diff_mappings(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Return ``{key: {from, to}}`` for keys whose values changed."""
    keys = set(before) | set(after)
    changed: dict[str, dict[str, Any]] = {}
    for key in sorted(keys):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changed[key] = {"from": old, "to": new}
    return changed


_LANGSMITH_ENV_KEYS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGSMITH_PROJECT",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
)


def _snapshot_langsmith_env() -> dict[str, str | None]:
    """Capture current LangSmith-related env values (None if unset)."""
    return {key: os.environ.get(key) for key in _LANGSMITH_ENV_KEYS}


def _restore_langsmith_env(previous: Mapping[str, str | None]) -> None:
    """Restore LangSmith env keys; pop any that were previously unset."""
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _resolved_langsmith_api_key() -> str | None:
    """Return a non-blank LangSmith API key from settings, or None."""
    settings = get_settings()
    if settings.langsmith_api_key is None:
        return None
    value = settings.langsmith_api_key.get_secret_value().strip()
    return value or None


def _resolved_langsmith_endpoint() -> str:
    """Return the LangSmith API base URL from settings (non-blank)."""
    settings = get_settings()
    endpoint = (settings.langsmith_endpoint or "").strip()
    return endpoint or "https://api.smith.langchain.com"


@contextmanager
def tracing(enabled: bool) -> Iterator[None]:
    """Scope LangSmith tracing to a single run.

    LangChain reads these from the environment at call time, so toggling the
    variables around the run is what makes this a per-profile switch. LangSmith
    env keys are snapshotted and restored in ``finally`` so a run cannot leave
    tracing credentials stuck in the process environment.

    Both ``enabled`` (profile.tracing_enabled) and
    ``JARVIS_LANGSMITH_TRACING_V2`` must be true; a key is also required.
    """
    settings = get_settings()
    process_on = bool(
        getattr(
            settings,
            "langsmith_tracing_v2",
            getattr(settings, "langsmith_tracing", False),
        )
    )
    api_key = _resolved_langsmith_api_key()
    tracing_on = bool(enabled and process_on and api_key)
    previous = _snapshot_langsmith_env()
    if not tracing_on:
        if enabled and process_on and not api_key:
            logger.warning(
                "LangSmith tracing requested but JARVIS_LANGSMITH_API_KEY is unset"
            )
        os.environ.pop("LANGSMITH_TRACING", None)
        os.environ.pop("LANGSMITH_TRACING_V2", None)
        try:
            yield
        finally:
            _restore_langsmith_env(previous)
        return

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_TRACING_V2"] = "true"
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = _resolved_langsmith_endpoint()
    os.environ["LANGSMITH_API_KEY"] = api_key
    try:
        # Explicit callback context is the most reliable path with current
        # LangChain/LangSmith versions; env vars remain for compatibility.
        client = LangSmithClient(
            api_key=api_key,
            api_url=os.environ["LANGSMITH_ENDPOINT"],
        )
        with tracing_v2_enabled(
            project_name=settings.langsmith_project,
            client=client,
        ):
            yield
    finally:
        _restore_langsmith_env(previous)
