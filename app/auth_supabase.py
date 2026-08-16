"""Supabase Auth JWT verification via the Auth HTTP API (httpx only).

Validates Bearer access tokens by calling ``GET /auth/v1/user`` with a short
in-process TTL cache keyed by token hash. No JWT library dependency.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

from app.config import Settings, get_settings
from app.monitoring import logger

# Soft cache so chat SSE / polls do not hit Supabase on every frame.
_CACHE_TTL_SECONDS = 45.0
_CACHE_MAX = 2048


@dataclass(frozen=True, slots=True)
class SupabaseUser:
    """Minimal identity extracted from a validated Supabase access token."""

    id: str
    email: str | None = None


@dataclass
class _CacheEntry:
    """Cached validation result with expiry."""

    user: SupabaseUser | None
    expires_at: float


_cache: dict[str, _CacheEntry] = {}


def supabase_auth_configured(settings: Settings | None = None) -> bool:
    """True when URL + anon (or service) key are set for Auth verification."""
    s = settings or get_settings()
    url = (s.supabase_url or "").strip()
    key = s.resolved_supabase_anon_key()
    return bool(url and key)


def _token_digest(token: str) -> str:
    """Hash a bearer token for cache keys (never log the raw token)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_get(digest: str) -> SupabaseUser | None | object:
    """Return cached user, None (invalid), or a sentinel when missing/expired."""
    entry = _cache.get(digest)
    if entry is None:
        return _MISS
    if entry.expires_at < time.monotonic():
        _cache.pop(digest, None)
        return _MISS
    return entry.user


_MISS = object()


def _cache_set(digest: str, user: SupabaseUser | None) -> None:
    """Store a validation result; drop oldest keys when over cap."""
    if len(_cache) >= _CACHE_MAX:
        # Drop roughly half the expired / oldest entries cheaply.
        now = time.monotonic()
        stale = [k for k, v in _cache.items() if v.expires_at < now]
        for key in stale[: max(1, _CACHE_MAX // 4)]:
            _cache.pop(key, None)
        if len(_cache) >= _CACHE_MAX:
            for key in list(_cache.keys())[: _CACHE_MAX // 4]:
                _cache.pop(key, None)
    _cache[digest] = _CacheEntry(
        user=user, expires_at=time.monotonic() + _CACHE_TTL_SECONDS
    )


def clear_supabase_auth_cache() -> None:
    """Drop the in-process token cache (tests / shutdown)."""
    _cache.clear()


async def verify_supabase_access_token(
    token: str, *, settings: Settings | None = None
) -> SupabaseUser | None:
    """Validate ``token`` against Supabase Auth; return user or None.

    Network or Auth errors fail closed (None). Blank tokens are invalid.
    """
    trimmed = token.strip()
    if not trimmed:
        return None

    s = settings or get_settings()
    if not supabase_auth_configured(s):
        return None

    digest = _token_digest(trimmed)
    cached = _cache_get(digest)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]

    base = s.supabase_url.rstrip("/")
    anon = s.resolved_supabase_anon_key()
    assert anon is not None  # guarded by supabase_auth_configured
    url = f"{base}/auth/v1/user"
    headers = {
        "Authorization": f"Bearer {trimmed}",
        "apikey": anon,
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Supabase Auth verify request failed: %s", type(exc).__name__)
        return None

    if response.status_code != 200:
        _cache_set(digest, None)
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    user_id = payload.get("id")
    if not isinstance(user_id, str) or not user_id.strip():
        _cache_set(digest, None)
        return None

    email_raw = payload.get("email")
    email = email_raw.strip() if isinstance(email_raw, str) and email_raw.strip() else None
    user = SupabaseUser(id=user_id.strip(), email=email)
    _cache_set(digest, user)
    return user


def rate_limit_user_key(token: str) -> str:
    """Stable per-token rate-limit bucket key (hash only)."""
    return f"user:{_token_digest(token)}"
