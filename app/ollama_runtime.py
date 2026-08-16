"""Ollama keep-alive helpers and optional boot-time model warm."""

from __future__ import annotations

import re

import httpx

from app.config import get_settings
from app.models import Profile, Provider
from app.monitoring import logger

_DURATION_RE = re.compile(
    r"^(-?\d+)(ms|s|m|h|d)?$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    None: 1,
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def normalize_keep_alive(value: str | int | None) -> str | int | None:
    """Return a non-empty Ollama keep_alive, or None when unset/blank."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    stripped = value.strip()
    return stripped or None


def keep_alive_as_seconds(value: str | int | None) -> int | None:
    """Convert Ollama keep_alive to seconds for APIs that only accept int.

    Supports ``-1`` (forever), bare second counts, and duration suffixes
    (``30m``, ``1h``, ``500ms``). Returns None when unset/blank/invalid.
    """
    normalized = normalize_keep_alive(value)
    if normalized is None:
        return None
    if isinstance(normalized, int):
        return normalized
    match = _DURATION_RE.fullmatch(normalized)
    if match is None:
        logger.warning("Ignoring invalid Ollama keep_alive %r", value)
        return None
    amount = int(match.group(1))
    if amount < 0:
        return -1
    unit = (match.group(2) or "").lower() or None
    seconds = amount * _UNIT_SECONDS[unit]
    return int(seconds)


def warm_ollama_model(model: str, keep_alive: str | int | None = None) -> bool:
    """Ask Ollama to load ``model`` into VRAM without a real generation.

    Returns True when the load request succeeds. Best-effort: never raises.
    """
    settings = get_settings()
    resolved = normalize_keep_alive(keep_alive)
    if resolved is None:
        resolved = normalize_keep_alive(settings.ollama_keep_alive)
    payload: dict = {"model": model, "stream": False}
    if resolved is not None:
        payload["keep_alive"] = resolved
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/generate",
                json=payload,
            )
            response.raise_for_status()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        logger.warning("Ollama warm failed for %s (%s)", model, exc)
        return False
    logger.info("Warmed Ollama model %s (keep_alive=%r)", model, resolved)
    return True


def warm_ollama_from_profile(profile: Profile) -> None:
    """Warm chat (+ embed when Ollama) so the first user turn skips cold load.

    Voice is intentionally skipped: loading chat and voice together on a tight
    GPU just causes the VRAM thrash this helper exists to avoid.
    """
    settings = get_settings()
    if not settings.ollama_warm_on_boot:
        return
    if profile.chat_provider is Provider.OLLAMA and profile.chat_model.strip():
        warm_ollama_model(profile.chat_model.strip(), settings.ollama_keep_alive)
    if (
        profile.embedding_provider is Provider.OLLAMA
        and profile.embedding_model.strip()
    ):
        warm_ollama_model(
            profile.embedding_model.strip(),
            settings.ollama_keep_alive,
        )
