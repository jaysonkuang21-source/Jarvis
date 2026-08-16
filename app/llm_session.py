"""Request-scoped LLM credentials for demo BYOK (bring your own key).

Credentials live only in a :class:`~contextvars.ContextVar` for the duration
of one request/stream. They are never written to disk, Settings, or caches.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from collections.abc import Iterator

# Header names (also used by CORS allow-list and the frontend client).
USER_LLM_KEY_HEADER = "X-Jarvis-User-LLM-Key"
USER_LLM_BASE_URL_HEADER = "X-Jarvis-User-LLM-Base-Url"

_llm_api_key: ContextVar[str | None] = ContextVar("jarvis_llm_api_key", default=None)
_llm_base_url: ContextVar[str | None] = ContextVar("jarvis_llm_base_url", default=None)


def get_request_llm_api_key() -> str | None:
    """Return the per-request OpenAI-compatible API key, if set."""
    value = _llm_api_key.get()
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def get_request_llm_base_url() -> str | None:
    """Return the optional per-request OpenAI-compatible base URL, if set."""
    value = _llm_base_url.get()
    if value is None:
        return None
    trimmed = value.strip().rstrip("/")
    return trimmed or None


@contextmanager
def request_llm_credentials(
    api_key: str | None,
    base_url: str | None = None,
) -> Iterator[None]:
    """Bind LLM credentials for the current context; clear on exit."""
    key_token: Token[str | None] = _llm_api_key.set(
        api_key.strip() if api_key and api_key.strip() else None
    )
    url_token: Token[str | None] = _llm_base_url.set(
        base_url.strip().rstrip("/") if base_url and base_url.strip() else None
    )
    try:
        yield
    finally:
        _llm_api_key.reset(key_token)
        _llm_base_url.reset(url_token)


def parse_user_llm_headers(headers: object) -> tuple[str | None, str | None]:
    """Extract BYOK key and optional base URL from a Starlette-like header map.

    Never log the returned key. Empty strings become ``None``.
    """
    get = getattr(headers, "get", None)
    if get is None:
        return None, None
    raw_key = get(USER_LLM_KEY_HEADER) or get(USER_LLM_KEY_HEADER.lower()) or ""
    raw_url = (
        get(USER_LLM_BASE_URL_HEADER) or get(USER_LLM_BASE_URL_HEADER.lower()) or ""
    )
    key = str(raw_key).strip() or None
    url = str(raw_url).strip().rstrip("/") or None
    return key, url
