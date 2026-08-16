"""Local Fish Speech TTS client (OpenAudio S1-mini over HTTP).

Fish Speech runs as a separate process/container. Jarvis only POSTs text to
``/v1/tts`` via httpx — no torch / fish-speech package in this venv.

Streaming mode requests ``streaming=true`` + ``format=wav``. Fish's API server
filters non-bytes yields, so the HTTP body is raw **PCM s16le mono** segment
bytes (not a complete WAV container). Non-streaming still returns a full WAV.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urljoin

import httpx

from app.config import get_settings
from app.models import TtsStatus

logger = logging.getLogger(__name__)

_MAX_CHARS = 4000
_ENGINE = "fish-speech"
_MODEL = "openaudio-s1-mini"
_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_http: httpx.Client | None = None


def _http_client(timeout: float) -> httpx.Client:
    """Reuse one httpx client so sentence-to-sentence Fish calls skip reconnect cost."""
    global _http
    if _http is None:
        _http = httpx.Client(timeout=timeout)
    return _http


def strip_think_tags(text: str) -> str:
    """Remove model think/reasoning traces so they are never spoken.

    Complete ``<think>…</think>`` blocks are dropped. Orphan closers are
    scrubbed. An unclosed opener truncates so hidden reasoning is not spoken.
    """
    text = _THINK_BLOCK.sub(" ", text)
    text = re.sub(r"</think>", " ", text, flags=re.IGNORECASE)
    lower = text.lower()
    open_at = lower.rfind("<think>")
    if open_at >= 0:
        text = text[:open_at]
    return text


def strip_for_speech(text: str) -> str:
    """Remove think tags and common markdown so TTS reads natural prose."""
    text = strip_think_tags(text)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~|>]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _base_url() -> str:
    """Normalized Fish Speech API root (no trailing slash)."""
    return get_settings().tts_base_url.strip().rstrip("/")


def _tts_url() -> str:
    """Full URL for the Fish Speech synthesize endpoint."""
    return urljoin(_base_url() + "/", "v1/tts")


def _prepare_text(text: str) -> str:
    """Strip markup and enforce the Fish text length cap."""
    clean = strip_for_speech(text)
    if not clean:
        raise ValueError("Nothing to speak after stripping markup")
    if len(clean) > _MAX_CHARS:
        clean = clean[: _MAX_CHARS - 1].rstrip() + "…"
    return clean


def _tts_payload(clean: str, *, streaming: bool) -> dict[str, object]:
    """Build the Fish ``/v1/tts`` JSON body for full-file or streamed PCM."""
    settings = get_settings()
    payload: dict[str, object] = {
        "text": clean,
        # Fish only allows streaming with format=wav; body is still raw PCM
        # segments when streaming=true (see module docstring).
        "format": "wav",
        "streaming": streaming,
        "seed": settings.tts_seed,
        "temperature": settings.tts_temperature,
        "top_p": settings.tts_top_p,
        "repetition_penalty": 1.2,
        "normalize": True,
        # Balanced favors earlier first audio on local Fish APIs.
        "latency": "balanced",
        "chunk_length": max(100, min(300, settings.tts_chunk_length)),
        "use_memory_cache": "on",
    }
    ref = (settings.tts_reference_id or "").strip()
    if ref:
        payload["reference_id"] = ref
    return payload


def fish_reachable() -> bool:
    """True when the Fish Speech HTTP server answers quickly."""
    settings = get_settings()
    if not settings.tts_enabled:
        return False
    try:
        with httpx.Client(timeout=settings.tts_probe_timeout_seconds) as client:
            # Prefer /v1/health (pinned S1 image); fall back to /docs.
            for path in ("v1/health", "docs"):
                try:
                    response = client.get(urljoin(_base_url() + "/", path))
                    if response.status_code < 500:
                        return True
                except httpx.HTTPError:
                    continue
    except httpx.HTTPError:
        return False
    return False


def status() -> TtsStatus:
    """Report whether Fish TTS is enabled and the local server is up."""
    settings = get_settings()
    return TtsStatus(
        enabled=settings.tts_enabled,
        ready=fish_reachable() if settings.tts_enabled else False,
        engine=_ENGINE,
        model=_MODEL,
        base_url=_base_url(),
    )


def warm_voice() -> None:
    """Ensure Fish Speech is up (autostart Docker if needed), then log status."""
    settings = get_settings()
    if not settings.tts_enabled:
        return
    from app.tts.server import ensure_fish_speech

    ready = ensure_fish_speech()
    if ready:
        logger.info(
            "Fish Speech TTS reachable at %s (model=%s)",
            _base_url(),
            _MODEL,
        )
    else:
        logger.warning(
            "Fish Speech TTS not ready at %s — spoken replies will use "
            "Web Speech (see scripts/fish-speech-up.ps1)",
            _base_url(),
        )


def synthesize_wav(text: str) -> bytes:
    """Ask the local Fish Speech server for a complete WAV (non-streaming).

    Raises ``RuntimeError`` when TTS is disabled or the server fails,
    ``ValueError`` when cleaned text is empty.
    """
    settings = get_settings()
    if not settings.tts_enabled:
        raise RuntimeError("TTS is disabled (JARVIS_TTS_ENABLED=false)")

    clean = _prepare_text(text)
    payload = _tts_payload(clean, streaming=False)

    try:
        client = _http_client(settings.tts_timeout_seconds)
        client.timeout = httpx.Timeout(settings.tts_timeout_seconds)
        response = client.post(_tts_url(), json=payload)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Fish Speech unreachable at {_tts_url()}: {exc}"
        ) from exc

    if response.status_code >= 400:
        detail = (response.text or response.reason_phrase)[:300]
        raise RuntimeError(
            f"Fish Speech TTS failed ({response.status_code}): {detail}"
        )

    audio = response.content
    if not audio:
        raise RuntimeError("Fish Speech returned empty audio")
    return audio


def iter_synthesize_pcm(text: str) -> Iterator[bytes]:
    """Stream raw PCM s16le mono bytes from Fish as soon as segments exist.

    Fish ``streaming=true`` yields decoded int16 segments over chunked HTTP.
    Raises the same errors as ``synthesize_wav`` before or during the stream.
    """
    settings = get_settings()
    if not settings.tts_enabled:
        raise RuntimeError("TTS is disabled (JARVIS_TTS_ENABLED=false)")

    clean = _prepare_text(text)
    payload = _tts_payload(clean, streaming=True)

    try:
        client = _http_client(settings.tts_timeout_seconds)
        client.timeout = httpx.Timeout(settings.tts_timeout_seconds)
        with client.stream("POST", _tts_url(), json=payload) as response:
            if response.status_code >= 400:
                detail = (response.read() or b"")[:300].decode(
                    "utf-8", errors="replace"
                )
                raise RuntimeError(
                    f"Fish Speech TTS failed ({response.status_code}): {detail}"
                )
            got = False
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                got = True
                yield chunk
            if not got:
                raise RuntimeError("Fish Speech returned empty audio")
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Fish Speech unreachable at {_tts_url()}: {exc}"
        ) from exc


async def aiter_synthesize_pcm(text: str) -> AsyncIterator[bytes]:
    """Async variant of ``iter_synthesize_pcm`` for FastAPI StreamingResponse."""
    settings = get_settings()
    if not settings.tts_enabled:
        raise RuntimeError("TTS is disabled (JARVIS_TTS_ENABLED=false)")

    clean = _prepare_text(text)
    payload = _tts_payload(clean, streaming=True)
    timeout = httpx.Timeout(settings.tts_timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", _tts_url(), json=payload) as response:
                if response.status_code >= 400:
                    detail = (await response.aread())[:300].decode(
                        "utf-8", errors="replace"
                    )
                    raise RuntimeError(
                        f"Fish Speech TTS failed ({response.status_code}): {detail}"
                    )
                got = False
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    got = True
                    yield chunk
                if not got:
                    raise RuntimeError("Fish Speech returned empty audio")
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Fish Speech unreachable at {_tts_url()}: {exc}"
        ) from exc
