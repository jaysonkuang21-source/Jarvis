"""Obsidian Local REST API client.

The plugin only answers while Obsidian is running, so this is never the
indexing path -- indexing reads the vault from disk. What the plugin adds is
the UI side: opening a cited note in the app, and atomic patches that touch one
heading instead of rewriting a file.

Every call degrades to an ``obsidian://`` URI the frontend can open instead.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from app.config import get_settings
from app.monitoring import logger


class ObsidianClient:
    def __init__(self) -> None:
        """Read REST URL and API key from settings; no network I/O yet."""
        settings = get_settings()
        self._base = settings.obsidian_rest_url.rstrip("/")
        self._key = (
            settings.obsidian_api_key.get_secret_value()
            if settings.obsidian_api_key
            else None
        )
        self._verify = settings.obsidian_verify_tls

    @property
    def configured(self) -> bool:
        """True when an API key is present; does not prove Obsidian is running."""
        return self._key is not None

    def _headers(self) -> dict[str, str]:
        """Bearer auth headers, or empty when no key is configured."""
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    async def available(self) -> bool:
        """Probe the plugin with a short timeout; False on any failure."""
        if not self.configured:
            return False
        try:
            # Keep health probes snappy — Obsidian offline must not stall ~2s.
            async with httpx.AsyncClient(timeout=0.4, verify=self._verify) as client:
                response = await client.get(f"{self._base}/", headers=self._headers())
                return response.status_code < 400
        except httpx.HTTPError:
            return False

    async def open_note(self, vault_relative_path: str) -> bool:
        """Bring a note to the front in Obsidian. False means use the URI."""
        if not self.configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=3.0, verify=self._verify) as client:
                response = await client.post(
                    f"{self._base}/open/{quote(vault_relative_path)}",
                    headers=self._headers(),
                )
                return response.status_code < 400
        except httpx.HTTPError as exc:
            logger.info("Obsidian open failed, falling back to URI (%s)", exc)
            return False


def obsidian_uri(vault_name: str, vault_relative_path: str) -> str:
    """Deep link that works without the plugin, as long as Obsidian is installed."""
    return (
        "obsidian://open"
        f"?vault={quote(vault_name, safe='')}"
        f"&file={quote(vault_relative_path, safe='')}"
    )


_client: ObsidianClient | None = None


def get_obsidian_client() -> ObsidianClient:
    """Return the shared Obsidian client, creating it on first use."""
    global _client
    if _client is None:
        _client = ObsidianClient()
    return _client
