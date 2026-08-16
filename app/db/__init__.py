"""Postgres connection helpers and schema for the hybrid index."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from app.config import (
    UnsafeDatabaseUrl,
    assert_safe_database_url,
    database_url_log_label,
    get_settings,
)
from app.monitoring import logger

_pool: Any | None = None
_schema_ready = False

# Short connect timeout so a dead Docker host cannot hang process boot.
_CONNECT_TIMEOUT_S = 5
# Pool checkout budget; keep below the historical 30s default.
_POOL_TIMEOUT_S = 10.0

_T = TypeVar("_T")


def database_configured() -> bool:
    """True when a Postgres URL is set in settings."""
    return bool(get_settings().database_url.strip())


def _validated_conninfo() -> str:
    """Normalize settings URL and enforce scheme + loopback policy."""
    settings = get_settings()
    return assert_safe_database_url(
        settings.database_url,
        allow_non_loopback=settings.allow_non_loopback,
    )


def get_pool():
    """Return a process-wide ConnectionPool, creating it on first use."""
    global _pool
    if _pool is not None:
        return _pool
    if not database_configured():
        raise RuntimeError("JARVIS_DATABASE_URL is not set")
    from psycopg_pool import ConnectionPool

    url = _validated_conninfo()
    _pool = ConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=8,
        open=True,
        timeout=_POOL_TIMEOUT_S,
        kwargs={"connect_timeout": _CONNECT_TIMEOUT_S},
    )
    return _pool


@contextmanager
def connection() -> Iterator[Any]:
    """Borrow a pooled connection."""
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def close_pool() -> None:
    """Shut down the pool (app lifespan)."""
    global _pool, _schema_ready
    if _pool is not None:
        _pool.close()
        _pool = None
    _schema_ready = False


def _is_schema_gap(exc: BaseException) -> bool:
    """True when tables are missing or the txn aborted after a missing relation."""
    try:
        from psycopg.errors import InFailedSqlTransaction, UndefinedTable
    except ImportError:  # pragma: no cover - psycopg always present in app use
        UndefinedTable = type(None)  # type: ignore[misc, assignment]
        InFailedSqlTransaction = type(None)  # type: ignore[misc, assignment]

    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (UndefinedTable, InFailedSqlTransaction)):
            return True
        name = type(cur).__name__
        if name in {"UndefinedTable", "InFailedSqlTransaction"}:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def clear_schema_ready() -> None:
    """Forget the process-local schema flag so the next ensure recreates DDL."""
    global _schema_ready
    _schema_ready = False


def run_with_schema(op: Callable[[], _T]) -> _T:
    """Run ``op`` after ensure_schema; once on UndefinedTable recreate and retry.

    Repo hot paths use this so a sticky ``_schema_ready`` cannot hide dropped
    tables forever after an external wipe or partial rebuild.
    """
    ensure_schema()
    try:
        return op()
    except Exception as exc:
        if not _is_schema_gap(exc):
            raise
        clear_schema_ready()
        logger.warning(
            "Postgres schema missing (%s); recreating and retrying once",
            type(exc).__name__,
        )
        ensure_schema()
        return op()


def check_postgres_ready() -> bool:
    """True when Postgres answers ``SELECT 1`` and the ``vector`` extension exists.

    Returns False when no URL is configured or on any connection / query error.
    Does not create the pool permanently on failure paths that raise before open.
    """
    if not database_configured():
        return False
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
            row = conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            return row is not None
    except Exception as exc:  # noqa: BLE001
        label = database_url_log_label(get_settings().database_url)
        logger.info(
            "Postgres readiness check failed (%s) target=%s",
            type(exc).__name__,
            label,
        )
        return False


# Default embedding width for qwen3-embedding:8b when the column does not exist yet.
# CREATE IF NOT EXISTS never changes an existing vector(N) — a dim/model mismatch
# requires an explicit destructive rebuild via rebuild_vector_schema().
DEFAULT_EMBED_DIMS = 4096

# Literal `{}` array defaults are doubled (`{{}}`) so str.format only
# substitutes `{dims}` — bare '{}' would raise IndexError.
SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    mtime DOUBLE PRECISION NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    tags TEXT[] NOT NULL DEFAULT '{{}}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    text TEXT NOT NULL,
    heading_path TEXT[] NOT NULL DEFAULT '{{}}',
    char_start INT NOT NULL DEFAULT 0,
    char_end INT NOT NULL DEFAULT 0,
    tsv TSVECTOR,
    embedding vector({dims}),
    UNIQUE (document_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks (document_id);
CREATE INDEX IF NOT EXISTS documents_tags_idx ON documents USING GIN (tags);

CREATE TABLE IF NOT EXISTS entities (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    embedding vector({dims}),
    UNIQUE (name_norm)
);

CREATE TABLE IF NOT EXISTS relationships (
    id BIGSERIAL PRIMARY KEY,
    src_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst_entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    rel_type TEXT NOT NULL DEFAULT 'related',
    source TEXT NOT NULL DEFAULT 'llm',
    evidence_chunk_ids BIGINT[] NOT NULL DEFAULT '{{}}',
    UNIQUE (src_entity_id, dst_entity_id, rel_type, source)
);

CREATE TABLE IF NOT EXISTS chunk_entities (
    chunk_id BIGINT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (chunk_id, entity_id)
);

CREATE TABLE IF NOT EXISTS communities (
    id BIGSERIAL PRIMARY KEY,
    level INT NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS community_members (
    community_id BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (community_id, entity_id)
);

CREATE TABLE IF NOT EXISTS community_reports (
    id BIGSERIAL PRIMARY KEY,
    community_id BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    level INT NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    embedding vector({dims})
);

CREATE TABLE IF NOT EXISTS index_meta (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    embedding_model TEXT,
    extraction_model TEXT,
    embedding_dims INT NOT NULL DEFAULT {dims},
    last_indexed_at TIMESTAMPTZ,
    indexing BOOLEAN NOT NULL DEFAULT FALSE,
    ready BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO index_meta (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
"""


# Additive migrations for databases created before tags / HNSW.
# Not passed through str.format — keep a single '{}' for Postgres.
_MIGRATIONS_SQL = """
ALTER TABLE documents ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS documents_tags_idx ON documents USING GIN (tags);
"""


def format_schema_sql(dims: int = DEFAULT_EMBED_DIMS) -> str:
    """Substitute embedding width into SCHEMA_SQL without touching literal braces."""
    return SCHEMA_SQL.format(dims=dims)


def ensure_schema(dims: int = DEFAULT_EMBED_DIMS) -> None:
    """Apply DDL once per process (idempotent CREATE IF NOT EXISTS).

    Existing ``vector(N)`` columns are not altered here. Call
    :func:`rebuild_vector_schema` when measured embedding width differs.
    """
    global _schema_ready
    if _schema_ready:
        return
    sql = format_schema_sql(dims)
    with connection() as conn:
        conn.execute(sql)
        conn.execute(_MIGRATIONS_SQL)
        # Commit base tables first. HNSW creation can fail (e.g. dims > 2000)
        # and must not abort the DDL transaction that created the tables.
        conn.commit()
        try:
            with conn.transaction():
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
                    ON chunks USING hnsw (embedding vector_cosine_ops)
                    """
                )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "HNSW index not created yet (%s). Exact / sequential search "
                "still works; ANN may be slower at dims=%s.",
                type(exc).__name__,
                dims,
            )
    _schema_ready = True
    logger.info("Postgres schema ready (dims=%s)", dims)


def rebuild_vector_schema(dims: int) -> None:
    """Destructively drop vector-backed tables and recreate at ``dims``.

    Loud on purpose: wrong-width upserts must never proceed quietly. Documents
    are wiped with chunks so the next reindex walk rewrites everything.
    """
    global _schema_ready
    logger.warning(
        "DESTROYING hybrid index tables to rebuild vector columns at dims=%s "
        "(embedding model/dim mismatch)",
        dims,
    )
    with connection() as conn:
        conn.execute("DROP TABLE IF EXISTS community_reports CASCADE")
        conn.execute("DROP TABLE IF EXISTS community_members CASCADE")
        conn.execute("DROP TABLE IF EXISTS communities CASCADE")
        conn.execute("DROP TABLE IF EXISTS chunk_entities CASCADE")
        conn.execute("DROP TABLE IF EXISTS relationships CASCADE")
        conn.execute("DROP TABLE IF EXISTS entities CASCADE")
        conn.execute("DROP TABLE IF EXISTS chunks CASCADE")
        conn.execute("DROP TABLE IF EXISTS documents CASCADE")
        conn.execute("DROP TABLE IF EXISTS index_meta CASCADE")
        conn.commit()
    _schema_ready = False
    try:
        from app.ingestion.dim_guard import clear_measured_embedding_cache

        clear_measured_embedding_cache()
    except Exception:  # noqa: BLE001
        pass
    ensure_schema(dims=dims)


def try_ensure_schema() -> bool:
    """Ensure schema when configured; return False on connection failure.

    URL policy errors (:class:`~app.config.UnsafeDatabaseUrl`) propagate so
    soft fallback cannot swallow a refused remote host or bad scheme.
    """
    if not database_configured():
        return False
    settings = get_settings()
    # Validate before opening the pool so policy failures are not soft-caught.
    assert_safe_database_url(
        settings.database_url,
        allow_non_loopback=settings.allow_non_loopback,
    )
    label = database_url_log_label(settings.database_url)
    try:
        ensure_schema()
        return True
    except UnsafeDatabaseUrl:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Postgres unavailable (%s) target=%s",
            type(exc).__name__,
            label,
        )
        return False
