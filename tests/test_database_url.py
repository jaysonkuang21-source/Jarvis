"""Database URL safety and soft/hard Postgres failure tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import (
    Settings,
    UnsafeDatabaseUrl,
    assert_safe_database_url,
    database_url_log_label,
    get_settings,
    normalize_database_url,
)
from app.obsidian import ObsidianClient


def test_normalize_strips_sqlalchemy_driver() -> None:
    """Driver suffixes become plain postgresql:// for psycopg."""
    assert (
        normalize_database_url("postgresql+psycopg://u:p@127.0.0.1:5432/jarvis")
        == "postgresql://u:p@127.0.0.1:5432/jarvis"
    )
    assert (
        normalize_database_url("postgres+psycopg://u:p@localhost/db")
        == "postgresql://u:p@localhost/db"
    )


def test_assert_safe_database_url_allows_loopback() -> None:
    """Loopback hosts are accepted without JARVIS_ALLOW_NON_LOOPBACK."""
    urls = (
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis",
        "postgresql://jarvis:secret@localhost:5432/jarvis",
        "postgresql://jarvis:secret@[::1]:5432/jarvis",
    )
    for url in urls:
        out = assert_safe_database_url(url, allow_non_loopback=False)
        assert out.startswith("postgresql://")


def test_assert_safe_database_url_refuses_remote_without_opt_in() -> None:
    """Non-loopback hosts raise unless explicitly opted in."""
    with pytest.raises(UnsafeDatabaseUrl, match="ALLOW_NON_LOOPBACK"):
        assert_safe_database_url(
            "postgresql://jarvis:secret@db.example.com:5432/jarvis",
            allow_non_loopback=False,
        )


def test_assert_safe_database_url_refuses_query_host_override() -> None:
    """Libpq ?host= / ?hostaddr= must not bypass the loopback authority gate."""
    poisoned = (
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis?host=evil.example.com",
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis?hostaddr=8.8.8.8",
        "postgresql://jarvis:secret@localhost:5432/jarvis?host=127.0.0.1,evil.com",
    )
    for url in poisoned:
        with pytest.raises(UnsafeDatabaseUrl, match="ALLOW_NON_LOOPBACK"):
            assert_safe_database_url(url, allow_non_loopback=False)


def test_assert_safe_database_url_allows_loopback_query_host() -> None:
    """Explicit loopback query targets remain allowed."""
    url = "postgresql://jarvis:secret@127.0.0.1:5432/jarvis?host=127.0.0.1"
    assert assert_safe_database_url(url, allow_non_loopback=False) == url


def test_assert_safe_database_url_allows_remote_with_opt_in() -> None:
    """JARVIS_ALLOW_NON_LOOPBACK opts into remote database hosts."""
    url = "postgresql://jarvis:secret@db.example.com:5432/jarvis"
    assert assert_safe_database_url(url, allow_non_loopback=True) == url
    overridden = (
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis?host=db.example.com"
    )
    assert assert_safe_database_url(overridden, allow_non_loopback=True) == overridden


def test_assert_safe_database_url_rejects_non_postgres_scheme() -> None:
    """Only postgresql:// and postgres:// survive after normalize."""
    with pytest.raises(UnsafeDatabaseUrl, match="scheme"):
        assert_safe_database_url(
            "mysql://jarvis:secret@127.0.0.1:3306/jarvis",
            allow_non_loopback=False,
        )
    with pytest.raises(UnsafeDatabaseUrl, match="scheme"):
        assert_safe_database_url(
            "sqlite:///./data/jarvis.db",
            allow_non_loopback=False,
        )


def test_assert_safe_database_url_accepts_postgres_scheme() -> None:
    """Bare postgres:// is allowed and left as-is after strip."""
    url = "postgres://jarvis:secret@127.0.0.1:5432/jarvis"
    assert assert_safe_database_url(url, allow_non_loopback=False) == url


def test_database_url_log_label_omits_password() -> None:
    """Log labels must never contain the password or username."""
    label = database_url_log_label(
        "postgresql://jarvis:s3cr3t-pass@127.0.0.1:5432/jarvis"
    )
    assert "s3cr3t-pass" not in label
    assert "jarvis:s3cr3t" not in label
    assert label == "127.0.0.1:5432/jarvis"


def test_database_url_log_label_uses_query_host() -> None:
    """Log target follows libpq ?host= override (still no credentials)."""
    label = database_url_log_label(
        "postgresql://jarvis:s3cr3t-pass@127.0.0.1:5432/jarvis?host=localhost"
    )
    assert "s3cr3t-pass" not in label
    assert label == "localhost:5432/jarvis"


def test_format_schema_sql_preserves_empty_array_defaults() -> None:
    """SCHEMA_SQL.format must not choke on Postgres DEFAULT '{}' braces."""
    from app.db import DEFAULT_EMBED_DIMS, _MIGRATIONS_SQL, format_schema_sql

    sql = format_schema_sql(384)
    assert "vector(384)" in sql
    assert "embedding_dims INT NOT NULL DEFAULT 384" in sql
    assert "DEFAULT '{}'" in sql
    assert "{{" not in sql
    # Migrations are executed raw (no .format); empty-array default stays intact.
    assert "DEFAULT '{}'" in _MIGRATIONS_SQL
    assert format_schema_sql().count(f"vector({DEFAULT_EMBED_DIMS})") == 3


def test_run_with_schema_retries_after_undefined_table(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sticky schema flag clears and DDL re-runs once on UndefinedTable."""
    monkeypatch.setenv(
        "JARVIS_DATABASE_URL",
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis",
    )
    get_settings.cache_clear()

    from app import db as db_mod

    db_mod.close_pool()
    # Pretend schema was already applied this process.
    db_mod._schema_ready = True
    calls = {"n": 0}

    class FakeUndefined(Exception):
        """Stand-in that matches by class name for _is_schema_gap."""

    FakeUndefined.__name__ = "UndefinedTable"

    def boom_then_ok() -> str:
        """Fail once with a missing-table error, then succeed."""
        calls["n"] += 1
        if calls["n"] == 1:
            raise FakeUndefined("relation missing")
        return "ok"

    ensure_calls = {"n": 0}

    def stub_ensure(dims: int = 4096) -> None:
        """Record ensure_schema calls without opening Postgres."""
        ensure_calls["n"] += 1
        db_mod._schema_ready = True

    with patch.object(db_mod, "ensure_schema", stub_ensure):
        assert db_mod.run_with_schema(boom_then_ok) == "ok"
    assert calls["n"] == 2
    assert ensure_calls["n"] >= 2
    db_mod.clear_schema_ready()


def test_try_ensure_schema_soft_failure_returns_false(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connect failures return False when database_required is false."""
    monkeypatch.setenv(
        "JARVIS_DATABASE_URL",
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis",
    )
    monkeypatch.setenv("JARVIS_DATABASE_REQUIRED", "false")
    get_settings.cache_clear()

    from app import db as db_mod

    db_mod.close_pool()

    with patch.object(db_mod, "ensure_schema", side_effect=OSError("boom")):
        assert db_mod.try_ensure_schema() is False


def test_try_ensure_schema_propagates_unsafe_url(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote host policy errors are not soft-swallowed."""
    monkeypatch.setenv(
        "JARVIS_DATABASE_URL",
        "postgresql://jarvis:secret@10.0.0.5:5432/jarvis",
    )
    monkeypatch.setenv("JARVIS_ALLOW_NON_LOOPBACK", "false")
    get_settings.cache_clear()

    from app import db as db_mod

    db_mod.close_pool()
    with pytest.raises(UnsafeDatabaseUrl, match="ALLOW_NON_LOOPBACK"):
        db_mod.try_ensure_schema()


def test_get_pool_passes_timeouts(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pool is opened with short connect and checkout timeouts."""
    monkeypatch.setenv(
        "JARVIS_DATABASE_URL",
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis",
    )
    get_settings.cache_clear()

    from app import db as db_mod

    db_mod.close_pool()
    fake_pool = MagicMock(name="ConnectionPool")
    with patch("psycopg_pool.ConnectionPool", return_value=fake_pool) as ctor:
        pool = db_mod.get_pool()
    assert pool is fake_pool
    kwargs = ctor.call_args.kwargs
    assert kwargs["timeout"] == db_mod._POOL_TIMEOUT_S
    assert kwargs["max_size"] == 8
    assert kwargs["kwargs"]["connect_timeout"] == db_mod._CONNECT_TIMEOUT_S
    db_mod.close_pool()


@pytest.fixture
def _stub_obsidian(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid real Obsidian probes during lifespan tests."""

    async def available(_self: ObsidianClient) -> bool:
        """Always report the plugin offline in these tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)


def test_lifespan_hard_fails_when_database_required(
    tmp_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    _stub_obsidian: None,
) -> None:
    """database_required=true refuses placeholder when URL is set but broken."""
    monkeypatch.setenv(
        "JARVIS_DATABASE_URL",
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis",
    )
    monkeypatch.setenv("JARVIS_DATABASE_REQUIRED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    monkeypatch.setattr("app.main.try_ensure_schema", lambda: False)
    monkeypatch.setattr("app.main.database_configured", lambda: True)

    from app.main import app

    with pytest.raises(RuntimeError, match="DATABASE_REQUIRED"):
        with TestClient(app):
            pass


def test_lifespan_soft_fallback_when_unreachable(
    tmp_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    _stub_obsidian: None,
) -> None:
    """database_required=false keeps the placeholder engine on connect failure."""
    monkeypatch.setenv(
        "JARVIS_DATABASE_URL",
        "postgresql://jarvis:secret@127.0.0.1:5432/jarvis",
    )
    monkeypatch.setenv("JARVIS_DATABASE_REQUIRED", "false")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    monkeypatch.setattr("app.main.try_ensure_schema", lambda: False)
    monkeypatch.setattr("app.main.database_configured", lambda: True)
    monkeypatch.setattr(
        "app.main.assert_safe_database_url",
        lambda url, *, allow_non_loopback: url,
    )
    monkeypatch.setattr("app.main.check_postgres_ready", lambda: False)

    from app.agent import PlaceholderRetrievalEngine
    from app.main import app

    with TestClient(app) as client:
        assert isinstance(client.app.state.engine, PlaceholderRetrievalEngine)
        health = client.get("/api/health").json()
        assert health["checks"]["postgres_configured"] is True
        assert health["checks"]["postgres_ready"] is False


def test_health_reports_postgres_unset(
    tmp_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    _stub_obsidian: None,
) -> None:
    """Unset URL reports configured=false and ready=false."""
    from app.main import app

    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["checks"]["postgres_configured"] is False
    assert body["checks"]["postgres_ready"] is False
