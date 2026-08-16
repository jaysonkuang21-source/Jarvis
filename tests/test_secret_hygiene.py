"""Secret hygiene: no hardcoded keys in source; public APIs never echo secrets."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from app.config import Settings, get_settings
from app.obsidian import ObsidianClient

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    REPO_ROOT / "app",
    REPO_ROOT / "frontend" / "src",
    REPO_ROOT / "scripts",
)
SCAN_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ps1",
    ".sh",
    ".bat",
    ".cmd",
    ".json",
    ".toml",
    ".md",
}

# OpenAI-style live keys (not the bare "sk-" substring, which appears in prose).
_HARDCODED_KEY = re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}")
# Literal assignments that look like committed secrets (empty/"..." placeholders OK).
_ASSIGNED_SECRET = re.compile(
    r"""(?ix)
    (?:openai_api_key|obsidian_api_key|langsmith_api_key|api_token|api_key)
    \s*=\s*
    (?:SecretStr\s*\(\s*)?
    ["']([^"']+)["']
    """
)
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "...",
        "changeme",
        "your-key-here",
        "your_api_key",
        "xxx",
        "test",
        "test-token",
        "test-token-ws4",
    }
)

# JSON keys that must never appear on public read endpoints.
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "openai_api_key",
        "obsidian_api_key",
        "langsmith_api_key",
        "api_token",
        "authorization",
        "x-jarvis-token",
    }
)

# Canaries planted on Settings — must never appear in response bodies.
_CANARY_OPENAI = "sk-canary-openai-key-NOTREAL0001"
_CANARY_OBSIDIAN = "obsidian-canary-secret-NOTREAL"
_CANARY_LANGSMITH = "lsv2-canary-secret-NOTREAL"
_CANARY_API_TOKEN = "jarvis-api-canary-token-NOTREAL"


def _iter_source_files() -> Iterator[Path]:
    """Yield text sources under app/, frontend/src/, and scripts/ (never .env)."""
    for root in SCAN_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SCAN_SUFFIXES:
                continue
            # Binary assets and generated noise stay out of the scan.
            if "node_modules" in path.parts or "dist" in path.parts:
                continue
            yield path


def test_no_hardcoded_secrets_in_source() -> None:
    """Fail if app/frontend/scripts commit key-shaped literals (skip .env)."""
    failures: list[str] = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in _HARDCODED_KEY.finditer(text):
            failures.append(f"{rel}: key-shaped literal near {match.group(0)[:12]}…")
        for match in _ASSIGNED_SECRET.finditer(text):
            value = match.group(1).strip()
            if value.lower() in _PLACEHOLDER_VALUES:
                continue
            if value.startswith("${") or value.startswith("%"):
                continue
            failures.append(f"{rel}: assigned secret-like value on {match.group(0).split('=')[0].strip()}")
    assert not failures, "Hardcoded secret patterns:\n" + "\n".join(failures)


def _walk_json(node: Any, path: str = "$") -> Iterator[tuple[str, str, Any]]:
    """Yield (json_path, key_or_empty, value) for every leaf and object key."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from _walk_json(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_json(item, f"{path}[{i}]")
    else:
        yield path, "", node


def _assert_public_json_clean(body: Any, *, canaries: list[str]) -> None:
    """Reject secret field names, sk-shaped values, canaries, and raw token echoes."""
    dumped = json.dumps(body, ensure_ascii=False)
    for canary in canaries:
        assert canary not in dumped, "Configured secret value leaked into API JSON"

    for json_path, key, value in _walk_json(body):
        if key and key.lower() in _FORBIDDEN_KEYS:
            raise AssertionError(f"Forbidden secret key {key!r} at {json_path}")
        if not isinstance(value, str):
            continue
        lower = value.lower()
        if "api_key" in lower:
            raise AssertionError(f"Value at {json_path} contains api_key")
        if _HARDCODED_KEY.search(value):
            raise AssertionError(f"Value at {json_path} looks like a live API key")
        # Raw token echo: the whole value equals a canary token, or a Bearer blob.
        if value in canaries:
            raise AssertionError(f"Raw secret token echoed at {json_path}")
        if lower.startswith("bearer ") and any(c in value for c in canaries):
            raise AssertionError(f"Bearer secret echoed at {json_path}")


@pytest.fixture
def leak_client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Boot the app with canary secrets via env (values never printed)."""
    assert tmp_settings.database_url == ""
    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", _CANARY_OPENAI)
    monkeypatch.setenv("JARVIS_OBSIDIAN_API_KEY", _CANARY_OBSIDIAN)
    monkeypatch.setenv("JARVIS_LANGSMITH_API_KEY", _CANARY_LANGSMITH)
    monkeypatch.setenv("JARVIS_API_TOKEN", _CANARY_API_TOKEN)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.resolved_api_token() == _CANARY_API_TOKEN
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()


@pytest.mark.parametrize(
    "method,path,headers",
    [
        ("GET", "/api/health", {}),
        (
            "GET",
            "/api/options",
            {"Authorization": f"Bearer {_CANARY_API_TOKEN}"},
        ),
        (
            "GET",
            "/api/profile",
            {"Authorization": f"Bearer {_CANARY_API_TOKEN}"},
        ),
        (
            "GET",
            "/api/rules",
            {"Authorization": f"Bearer {_CANARY_API_TOKEN}"},
        ),
        (
            "GET",
            "/api/metrics",
            {
                "Authorization": f"Bearer {_CANARY_API_TOKEN}",
                "X-Jarvis-Token": _CANARY_API_TOKEN,
            },
        ),
        (
            "GET",
            "/api/index/status",
            {"Authorization": f"Bearer {_CANARY_API_TOKEN}"},
        ),
    ],
)
def test_public_get_json_does_not_leak_secrets(
    leak_client: TestClient,
    method: str,
    path: str,
    headers: dict[str, str],
) -> None:
    """Authenticated/read JSON must not echo env secrets or secret-shaped strings."""
    response = leak_client.request(method, path, headers=headers)
    assert response.status_code == 200, f"{path} -> {response.status_code}"
    body = response.json()
    _assert_public_json_clean(
        body,
        canaries=[
            _CANARY_OPENAI,
            _CANARY_OBSIDIAN,
            _CANARY_LANGSMITH,
            _CANARY_API_TOKEN,
        ],
    )


def test_token_mode_rejects_unauthenticated_reads(leak_client: TestClient) -> None:
    """With a configured token, protected GETs must not succeed without auth."""
    for path in ("/api/options", "/api/profile", "/api/rules", "/api/metrics"):
        assert leak_client.get(path).status_code == 401
