"""Opt-in Hugging Face Hub model metrics with disk TTL cache.

Default OFF. Failures never block recommendations — callers treat misses as
neutral ``hf_signal`` and set ``metrics_degraded``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, Field

from app.config import get_settings
from app.monitoring import logger

HF_API_HOST = "huggingface.co"
HF_TTL_SECONDS = 24 * 60 * 60
# Repo ids are org/name with a limited charset — reject anything else.
_HF_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:/[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)+$")
# Only the Hub apex host — never www or other aliases for outbound fetches.
_ALLOWED_HF_HOSTS = frozenset({HF_API_HOST})


class HfModelMetrics(BaseModel):
    """Subset of HF Hub model card stats used by the role scorer."""

    hf_id: str
    downloads: int | None = None
    likes: int | None = None
    last_modified: str | None = None
    pipeline_tag: str | None = None
    fetched_at: float = Field(default_factory=lambda: time.time())
    from_cache: bool = False


def _cache_dir() -> Path:
    """Return ``data/cache/hf_metrics``, creating it if needed."""
    settings = get_settings()
    path = settings.data_dir / "cache" / "hf_metrics"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_cache_path(hf_id: str) -> Path | None:
    """Map an hf_id to a cache file under the metrics dir (hash only, no path join)."""
    if not is_safe_hf_id(hf_id):
        return None
    digest = hashlib.sha256(hf_id.encode("utf-8")).hexdigest()
    return _cache_dir() / f"{digest}.json"


def is_safe_hf_id(hf_id: str) -> bool:
    """Reject empty, absolute, traversal, or non-HF-shaped repo ids."""
    if not hf_id or not isinstance(hf_id, str):
        return False
    if ".." in hf_id or hf_id.startswith("/") or "\\" in hf_id:
        return False
    if len(hf_id) > 200:
        return False
    return bool(_HF_ID_RE.match(hf_id))


def _read_cache(hf_id: str) -> HfModelMetrics | None:
    """Return cached metrics when present and younger than the TTL."""
    path = _safe_cache_path(hf_id)
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        metrics = HfModelMetrics.model_validate(payload)
    except (OSError, ValueError):
        return None
    if time.time() - metrics.fetched_at > HF_TTL_SECONDS:
        return None
    metrics.from_cache = True
    return metrics


def _write_cache(metrics: HfModelMetrics) -> None:
    """Persist metrics to the TTL disk cache (best-effort)."""
    path = _safe_cache_path(metrics.hf_id)
    if path is None:
        return
    try:
        path.write_text(
            metrics.model_dump_json(),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.info("Could not write HF metrics cache (%s)", exc)


def _auth_headers() -> dict[str, str]:
    """Optional Bearer from JARVIS_HF_TOKEN; never logged."""
    settings = get_settings()
    token = settings.resolved_hf_token()
    headers = {"User-Agent": "jarvis"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _api_url(hf_id: str) -> str | None:
    """Build an allowlisted HF Hub API URL for a safe repo id."""
    if not is_safe_hf_id(hf_id):
        return None
    # quote keeps slash as path separator between org and name.
    encoded = quote(hf_id, safe="/")
    url = f"https://{HF_API_HOST}/api/models/{encoded}"
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HF_HOSTS
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    return url


def _response_host_allowed(response: httpx.Response) -> bool:
    """True when the final response URL host is the Hub allowlist only."""
    host = response.url.host
    return bool(host) and host in _ALLOWED_HF_HOSTS


def _parse_hub_payload(hf_id: str, payload: dict[str, Any]) -> HfModelMetrics:
    """Map Hub JSON fields into HfModelMetrics."""
    last_mod = payload.get("lastModified") or payload.get("last_modified")
    if isinstance(last_mod, datetime):
        last_mod = last_mod.astimezone(timezone.utc).isoformat()
    elif last_mod is not None:
        last_mod = str(last_mod)
    downloads = payload.get("downloads")
    likes = payload.get("likes")
    return HfModelMetrics(
        hf_id=hf_id,
        downloads=int(downloads) if downloads is not None else None,
        likes=int(likes) if likes is not None else None,
        last_modified=last_mod,
        pipeline_tag=payload.get("pipeline_tag"),
        fetched_at=time.time(),
        from_cache=False,
    )


async def fetch_hf_metrics(
    hf_id: str,
    *,
    client: httpx.AsyncClient | None = None,
    use_cache: bool = True,
) -> tuple[HfModelMetrics | None, bool]:
    """Fetch Hub stats for ``hf_id``.

    Returns ``(metrics_or_none, degraded)``. ``degraded`` is True on network,
    parse, or validation failure (or unsafe id).
    """
    if not is_safe_hf_id(hf_id):
        return None, True

    if use_cache:
        cached = _read_cache(hf_id)
        if cached is not None:
            return cached, False

    url = _api_url(hf_id)
    if url is None:
        return None, True

    owns_client = client is None
    if owns_client:
        # follow_redirects=False is required SSRF control; never open catalog/user URLs.
        client = httpx.AsyncClient(timeout=3.0, follow_redirects=False)

    assert client is not None
    try:
        response = await client.get(url, headers=_auth_headers())
        # Reject redirects and non-success even when a shared client follows them.
        if response.is_redirect or response.status_code != 200:
            return None, True
        if not _response_host_allowed(response):
            return None, True
        payload = response.json()
        if not isinstance(payload, dict):
            return None, True
        metrics = _parse_hub_payload(hf_id, payload)
        if use_cache:
            _write_cache(metrics)
        return metrics, False
    except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
        # Log exception type only — never headers/body (may contain Bearer).
        logger.info("HF metrics fetch failed for %s (%s)", hf_id, type(exc).__name__)
        return None, True
    finally:
        if owns_client:
            await client.aclose()


async def fetch_many(
    hf_ids: list[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict[str, HfModelMetrics], bool]:
    """Fetch metrics for many ids; any failure flips the degraded flag."""
    results: dict[str, HfModelMetrics] = {}
    degraded = False
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=3.0, follow_redirects=False)
    assert client is not None
    try:
        for hf_id in hf_ids:
            metrics, failed = await fetch_hf_metrics(hf_id, client=client)
            if metrics is not None:
                results[hf_id] = metrics
            if failed:
                degraded = True
        return results, degraded
    finally:
        if owns_client:
            await client.aclose()


def clear_hf_cache() -> None:
    """Delete all files under the HF metrics cache dir (tests)."""
    root = _cache_dir()
    for path in root.glob("*.json"):
        try:
            path.unlink()
        except OSError:
            continue
