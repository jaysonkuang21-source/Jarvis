"""PUT /api/rules elevation gate and cache-bust behaviour."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.obsidian import ObsidianClient


@pytest.fixture
def rules_client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Iterator[TestClient]:
    """API client with a writable temp rules.md and unauthenticated lab mode."""
    rules_path = tmp_path / "rules.md"
    # Minimal frontmatter mirroring defaults used by Policy.
    rules_path.write_text(
        "---\n"
        "version: 1\n"
        "allow_delete: false\n"
        "allow_download: false\n"
        "allow_shell: false\n"
        "allow_network: true\n"
        "allow_email_send: false\n"
        "allow_vault_write: true\n"
        'vault_path: ""\n'
        "allowed_read_paths:\n  - ./data\n"
        "allowed_write_paths:\n  - ./data\n"
        "denied_paths: []\n"
        "trash_dir: ./data/trash\n"
        "quarantine_dir: ./data/quarantine\n"
        "require_approval_for:\n  - file_write\n"
        "max_file_writes_per_turn: 5\n"
        "max_tool_calls_per_turn: 25\n"
        "max_download_bytes: 1000\n"
        "allowed_tools:\n  - vault_read\n  - vault_write\n  - file_download\n"
        "---\n\nTest policy body.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_RULES_PATH", str(rules_path))
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    # Reset process-wide policy engine against the temp rules file.
    import app.security as security_mod

    security_mod._engine = None

    from app.main import app

    with TestClient(app) as client:
        yield client

    security_mod._engine = None
    get_settings.cache_clear()


def test_put_rules_rejects_elevation_without_confirm(rules_client: TestClient) -> None:
    """Enabling allow_shell without confirm_elevation returns 403."""
    current = rules_client.get("/api/rules").json()
    current["allow_shell"] = True
    blocked = rules_client.put(
        "/api/rules", json={"policy": current, "confirm_elevation": False}
    )
    assert blocked.status_code == 403
    body = blocked.json()
    assert "confirm_elevation" in (body.get("error") or "") or "confirm_elevation" in (
        body.get("detail") or ""
    )


def test_put_rules_allows_elevation_with_confirm(rules_client: TestClient) -> None:
    """confirm_elevation=true persists a capability elevation."""
    current = rules_client.get("/api/rules").json()
    current["allow_download"] = True
    ok = rules_client.put(
        "/api/rules", json={"policy": current, "confirm_elevation": True}
    )
    assert ok.status_code == 200
    assert ok.json()["allow_download"] is True


def test_put_rules_non_elevating_edit_needs_no_confirm(rules_client: TestClient) -> None:
    """Lowering a budget is not elevation and saves without the flag."""
    current = rules_client.get("/api/rules").json()
    current["max_tool_calls_per_turn"] = 10
    ok = rules_client.put(
        "/api/rules", json={"policy": current, "confirm_elevation": False}
    )
    assert ok.status_code == 200
    assert ok.json()["max_tool_calls_per_turn"] == 10
