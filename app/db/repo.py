"""Repository helpers for the hybrid Postgres index."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, ParamSpec, TypeVar

from app.db import connection, ensure_schema, run_with_schema

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _schema_retry(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Retry the wrapped repo call once after sticky-schema / missing-table recovery."""

    @wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Delegate to ``run_with_schema`` so UndefinedTable can recreate DDL."""
        return run_with_schema(lambda: fn(*args, **kwargs))

    return wrapper


@_schema_retry
def set_indexing(flag: bool) -> None:
    """Flip the single-row indexing flag."""
    ensure_schema()
    with connection() as conn:
        conn.execute("UPDATE index_meta SET indexing = %s WHERE id = 1", (flag,))
        conn.commit()


@_schema_retry
def mark_ready(*, embedding_model: str, extraction_model: str, dims: int) -> None:
    """Record a successful index build."""
    ensure_schema()
    with connection() as conn:
        conn.execute(
            """
            UPDATE index_meta SET
                ready = TRUE,
                indexing = FALSE,
                embedding_model = %s,
                extraction_model = %s,
                embedding_dims = %s,
                last_indexed_at = %s
            WHERE id = 1
            """,
            (embedding_model, extraction_model, dims, datetime.now(timezone.utc)),
        )
        conn.commit()


@_schema_retry
def fetch_index_meta() -> dict[str, Any]:
    """Return the singleton index_meta row as a dict."""
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            """
            SELECT ready, indexing, embedding_model, extraction_model,
                   embedding_dims, last_indexed_at
            FROM index_meta WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return {
            "ready": False,
            "indexing": False,
            "embedding_model": None,
            "extraction_model": None,
            "embedding_dims": 4096,
            "last_indexed_at": None,
        }
    return {
        "ready": row[0],
        "indexing": row[1],
        "embedding_model": row[2],
        "extraction_model": row[3],
        "embedding_dims": row[4],
        "last_indexed_at": row[5],
    }


@_schema_retry
def mark_not_ready() -> None:
    """Clear the ready flag when the index must not answer queries."""
    ensure_schema()
    with connection() as conn:
        conn.execute("UPDATE index_meta SET ready = FALSE WHERE id = 1")
        conn.commit()


@_schema_retry
def measure_vector_column_dims(table: str = "chunks") -> int | None:
    """Read the actual pgvector column width from the catalog (not meta alone)."""
    ensure_schema()
    allowed = {"chunks", "entities", "community_reports"}
    if table not in allowed:
        raise ValueError(table)
    with connection() as conn:
        row = conn.execute(
            """
            SELECT a.atttypmod
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s
              AND a.attname = 'embedding'
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND n.nspname = current_schema()
            LIMIT 1
            """,
            (table,),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    typmod = int(row[0])
    # pgvector stores the dimension count directly in atttypmod (not dim+4).
    if typmod < 0:
        return None
    return typmod


def assert_embedding_width(embedding: list[float] | None) -> None:
    """Refuse upserts whose width does not match the live vector column."""
    if embedding is None:
        return
    column_dims = measure_vector_column_dims("chunks")
    if column_dims is None:
        return
    if len(embedding) != column_dims:
        msg = (
            f"Refusing upsert: embedding has {len(embedding)} dims but "
            f"chunks.embedding is vector({column_dims}). Reindex required."
        )
        raise ValueError(msg)


@_schema_retry
def count_table(table: str) -> int:
    """Count rows in a known index table."""
    ensure_schema()
    allowed = {
        "documents",
        "chunks",
        "entities",
        "relationships",
        "communities",
        "community_reports",
    }
    if table not in allowed:
        raise ValueError(table)
    with connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


@_schema_retry
def upsert_document(
    path: str,
    title: str,
    mtime: float,
    content_hash: str,
    tags: list[str] | None = None,
) -> int:
    """Insert or update a document; return its id."""
    ensure_schema()
    tag_list = list(tags or [])
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO documents (path, title, mtime, content_hash, tags)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (path) DO UPDATE SET
                title = EXCLUDED.title,
                mtime = EXCLUDED.mtime,
                content_hash = EXCLUDED.content_hash,
                tags = EXCLUDED.tags,
                updated_at = NOW()
            RETURNING id
            """,
            (path, title, mtime, content_hash, tag_list),
        ).fetchone()
        conn.commit()
    return int(row[0])


@_schema_retry
def delete_document_chunks(document_id: int) -> None:
    """Remove chunks (and cascade links) for a document before re-chunking."""
    ensure_schema()
    with connection() as conn:
        conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        conn.commit()


@_schema_retry
def insert_chunk(
    *,
    document_id: int,
    chunk_id: str,
    text: str,
    heading_path: list[str],
    char_start: int,
    char_end: int,
    embedding: list[float] | None,
) -> int:
    """Insert one evidence chunk with optional embedding and FTS vector."""
    ensure_schema()
    assert_embedding_width(embedding)
    emb = None if embedding is None else "[" + ",".join(str(float(x)) for x in embedding) + "]"
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO chunks (
                document_id, chunk_id, text, heading_path,
                char_start, char_end, tsv, embedding
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                to_tsvector('english', %s),
                %s::vector
            )
            ON CONFLICT (document_id, chunk_id) DO UPDATE SET
                text = EXCLUDED.text,
                heading_path = EXCLUDED.heading_path,
                char_start = EXCLUDED.char_start,
                char_end = EXCLUDED.char_end,
                tsv = EXCLUDED.tsv,
                embedding = EXCLUDED.embedding
            RETURNING id
            """,
            (
                document_id,
                chunk_id,
                text,
                heading_path,
                char_start,
                char_end,
                text,
                emb,
            ),
        ).fetchone()
        conn.commit()
    return int(row[0])


@_schema_retry
def upsert_entity(name: str, description: str = "") -> int:
    """Insert or return an entity by normalized name."""
    ensure_schema()
    norm = name.strip().lower()
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO entities (name, name_norm, description)
            VALUES (%s, %s, %s)
            ON CONFLICT (name_norm) DO UPDATE SET
                name = EXCLUDED.name,
                description = CASE
                    WHEN EXCLUDED.description <> '' THEN EXCLUDED.description
                    ELSE entities.description
                END
            RETURNING id
            """,
            (name.strip(), norm, description),
        ).fetchone()
        conn.commit()
    return int(row[0])


@_schema_retry
def upsert_relationship(
    src_id: int,
    dst_id: int,
    rel_type: str,
    source: str,
    evidence_chunk_ids: list[int],
) -> None:
    """Insert a relationship edge, merging evidence chunk ids."""
    ensure_schema()
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO relationships (
                src_entity_id, dst_entity_id, rel_type, source, evidence_chunk_ids
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (src_entity_id, dst_entity_id, rel_type, source) DO UPDATE SET
                evidence_chunk_ids = (
                    SELECT ARRAY(SELECT DISTINCT unnest(
                        relationships.evidence_chunk_ids || EXCLUDED.evidence_chunk_ids
                    ))
                )
            """,
            (src_id, dst_id, rel_type, source, evidence_chunk_ids),
        )
        conn.commit()


@_schema_retry
def link_chunk_entity(chunk_db_id: int, entity_id: int) -> None:
    """Associate a chunk with an entity."""
    ensure_schema()
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO chunk_entities (chunk_id, entity_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
            """,
            (chunk_db_id, entity_id),
        )
        conn.commit()


@_schema_retry
def clear_communities() -> None:
    """Drop community tables before rebuilding."""
    ensure_schema()
    with connection() as conn:
        conn.execute("DELETE FROM community_reports")
        conn.execute("DELETE FROM community_members")
        conn.execute("DELETE FROM communities")
        conn.commit()


@_schema_retry
def create_community(level: int, label: str, member_ids: list[int], summary: str) -> int:
    """Create a community, its members, and a report."""
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO communities (level, label) VALUES (%s, %s) RETURNING id",
            (level, label),
        ).fetchone()
        cid = int(row[0])
        for eid in member_ids:
            conn.execute(
                """
                INSERT INTO community_members (community_id, entity_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                (cid, eid),
            )
        conn.execute(
            """
            INSERT INTO community_reports (community_id, level, summary)
            VALUES (%s, %s, %s)
            """,
            (cid, level, summary),
        )
        conn.commit()
    return cid


def _metadata_filter_sql(
    *,
    path_prefix: str | None = None,
    tags: list[str] | None = None,
) -> tuple[str, list[Any]]:
    """Build AND-clauses and bind values for document metadata filters."""
    rebuilt: list[str] = []
    params: list[Any] = []
    if path_prefix:
        rebuilt.append("d.path LIKE %s")
        params.append(path_prefix.rstrip("/") + "%")
    if tags:
        rebuilt.append("d.tags @> %s")
        params.append(list(tags))
    if not rebuilt:
        return "", []
    return " AND " + " AND ".join(rebuilt), params


@_schema_retry
def vector_search(
    embedding: list[float],
    limit: int,
    *,
    path_prefix: str | None = None,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Nearest-neighbor chunk search by embedding, optional metadata filters."""
    ensure_schema()
    emb = "[" + ",".join(str(float(x)) for x in embedding) + "]"
    extra, extra_params = _metadata_filter_sql(path_prefix=path_prefix, tags=tags)
    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.chunk_id, c.text, c.heading_path, c.char_start, c.char_end,
                   d.path, d.title,
                   c.embedding <=> %s::vector AS dist
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL{extra}
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (emb, *extra_params, emb, limit),
        ).fetchall()
    return [_chunk_row(r, score=1.0 / (1.0 + float(r[8]))) for r in rows]


@_schema_retry
def keyword_search(
    query: str,
    limit: int,
    *,
    path_prefix: str | None = None,
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Full-text search over chunk tsv, optional metadata filters."""
    ensure_schema()
    extra, extra_params = _metadata_filter_sql(path_prefix=path_prefix, tags=tags)
    with connection() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.chunk_id, c.text, c.heading_path, c.char_start, c.char_end,
                   d.path, d.title,
                   ts_rank(c.tsv, websearch_to_tsquery('english', %s)) AS rank
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.tsv @@ websearch_to_tsquery('english', %s){extra}
            ORDER BY rank DESC
            LIMIT %s
            """,
            (query, query, *extra_params, limit),
        ).fetchall()
    return [_chunk_row(r, score=float(r[8])) for r in rows]


@_schema_retry
def chunks_for_entities(entity_ids: list[int], limit: int = 50) -> list[dict[str, Any]]:
    """Evidence chunks linked to any of the given entities."""
    if not entity_ids:
        return []
    ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT c.id, c.chunk_id, c.text, c.heading_path, c.char_start, c.char_end,
                   d.path, d.title, 1.0 AS score
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            JOIN chunk_entities ce ON ce.chunk_id = c.id
            WHERE ce.entity_id = ANY(%s)
            LIMIT %s
            """,
            (entity_ids, limit),
        ).fetchall()
    return [_chunk_row(r, score=float(r[8])) for r in rows]


@_schema_retry
def neighbor_entity_ids(seed_ids: list[int], hops: int = 1) -> list[int]:
    """Expand entity ids along relationships for ``hops`` steps."""
    if not seed_ids:
        return []
    ensure_schema()
    found = set(seed_ids)
    frontier = set(seed_ids)
    with connection() as conn:
        for _ in range(max(1, hops)):
            if not frontier:
                break
            rows = conn.execute(
                """
                SELECT src_entity_id, dst_entity_id FROM relationships
                WHERE src_entity_id = ANY(%s) OR dst_entity_id = ANY(%s)
                """,
                (list(frontier), list(frontier)),
            ).fetchall()
            nxt: set[int] = set()
            for src, dst in rows:
                if src not in found:
                    nxt.add(src)
                if dst not in found:
                    nxt.add(dst)
            found |= nxt
            frontier = nxt
    return list(found)


@_schema_retry
def entity_ids_for_chunks(chunk_ids: list[int]) -> list[int]:
    """Entities linked to the given chunk row ids."""
    if not chunk_ids:
        return []
    ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT entity_id FROM chunk_entities WHERE chunk_id = ANY(%s)",
            (chunk_ids,),
        ).fetchall()
    return [int(r[0]) for r in rows]


@_schema_retry
def list_community_reports(level: int, limit: int = 50) -> list[dict[str, Any]]:
    """Community summaries at or below the requested level."""
    ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.community_id, r.level, r.summary, c.label
            FROM community_reports r
            JOIN communities c ON c.id = r.community_id
            WHERE r.level <= %s
            ORDER BY r.level, r.id
            LIMIT %s
            """,
            (level, limit),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "community_id": int(r[1]),
            "level": int(r[2]),
            "summary": r[3],
            "label": r[4],
        }
        for r in rows
    ]


@_schema_retry
def list_entities() -> list[tuple[int, str]]:
    """All entity ids and names for community detection."""
    ensure_schema()
    with connection() as conn:
        rows = conn.execute("SELECT id, name FROM entities ORDER BY id").fetchall()
    return [(int(r[0]), r[1]) for r in rows]


@_schema_retry
def list_relationship_pairs() -> list[tuple[int, int]]:
    """Undirected edge list for community detection."""
    ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            "SELECT src_entity_id, dst_entity_id FROM relationships"
        ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


@_schema_retry
def list_document_paths() -> list[str]:
    """Return every indexed vault-relative document path."""
    ensure_schema()
    with connection() as conn:
        rows = conn.execute("SELECT path FROM documents ORDER BY path").fetchall()
    return [str(row[0]) for row in rows]


@_schema_retry
def list_indexed_documents() -> list[dict[str, Any]]:
    """Return indexed documents with titles, tags, and chunk counts.

    Used by Settings so chunk inspection works across app sessions, not only
    for paths ingested in the current UI session.
    """
    ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT d.path, d.title, d.tags, COUNT(c.id) AS chunk_count
            FROM documents d
            LEFT JOIN chunks c ON c.document_id = d.id
            GROUP BY d.id
            ORDER BY d.path ASC
            """
        ).fetchall()
    return [
        {
            "path": str(row[0]),
            "title": str(row[1] or ""),
            "tags": _tag_list(row[2]),
            "chunk_count": int(row[3] or 0),
        }
        for row in rows
    ]


@_schema_retry
def delete_document_by_path(path: str) -> bool:
    """Delete one document row; chunks and links cascade. Returns True when removed."""
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "DELETE FROM documents WHERE path = %s RETURNING id",
            (path,),
        ).fetchone()
        conn.commit()
    return row is not None


@_schema_retry
def document_by_hash(path: str) -> tuple[int, str] | None:
    """Return (id, content_hash) for a path if indexed."""
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "SELECT id, content_hash FROM documents WHERE path = %s", (path,)
        ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1])


@_schema_retry
def list_chunks_for_path(path: str, limit: int = 500) -> tuple[int, list[dict[str, Any]]]:
    """Return (total_count, chunk dicts) for a vault-relative document path.

    Chunks are ordered by ``char_start`` then id. ``limit`` caps how many rows
    are returned in the list; ``total`` is always the full count. Each chunk
    includes document ``tags``, ``wikilinks`` parsed from text, and linked
    ``entities`` from extraction/wikilink edges.
    """
    from app.ingestion.chunkers import extract_wikilinks

    ensure_schema()
    normalized = path.strip().replace("\\", "/")
    cap = max(1, min(int(limit), 500))
    with connection() as conn:
        doc = conn.execute(
            "SELECT id, tags FROM documents WHERE path = %s", (normalized,)
        ).fetchone()
        if doc is None:
            return 0, []
        document_id = int(doc[0])
        doc_tags = _tag_list(doc[1])
        total_row = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE document_id = %s",
            (document_id,),
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        rows = conn.execute(
            """
            SELECT c.id, c.chunk_id, c.text, c.heading_path, c.char_start, c.char_end,
                   d.path, d.title
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.document_id = %s
            ORDER BY c.char_start ASC, c.id ASC
            LIMIT %s
            """,
            (document_id, cap),
        ).fetchall()
        db_ids = [int(r[0]) for r in rows]
        entities_by_db: dict[int, list[str]] = {i: [] for i in db_ids}
        if db_ids:
            ent_rows = conn.execute(
                """
                SELECT ce.chunk_id, e.name
                FROM chunk_entities ce
                JOIN entities e ON e.id = ce.entity_id
                WHERE ce.chunk_id = ANY(%s)
                ORDER BY e.name ASC
                """,
                (db_ids,),
            ).fetchall()
            for chunk_db_id, name in ent_rows:
                bucket = entities_by_db.setdefault(int(chunk_db_id), [])
                label = str(name or "").strip()
                if label and label not in bucket:
                    bucket.append(label)
    chunks = [
        {
            "chunk_id": str(r[1]),
            "text": str(r[2] or ""),
            "heading_path": _heading_list(r[3]),
            "char_start": int(r[4]),
            "char_end": int(r[5]),
            "note_path": str(r[6]),
            "note_title": str(r[7] or ""),
            "tags": list(doc_tags),
            "wikilinks": extract_wikilinks(str(r[2] or "")),
            "entities": list(entities_by_db.get(int(r[0]), [])),
        }
        for r in rows
    ]
    return total, chunks


def _tag_list(tags: Any) -> list[str]:
    """Normalize a documents.tags SQL value to a list of strings."""
    if tags is None:
        return []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            return [tags] if tags.strip() else []
    return [str(t).strip() for t in (tags or []) if str(t).strip()]


def _heading_list(heading: Any) -> list[str]:
    """Normalize a heading_path SQL value to a list of strings."""
    if isinstance(heading, str):
        try:
            heading = json.loads(heading)
        except json.JSONDecodeError:
            return [heading]
    return list(heading or [])


def _chunk_row(r: Any, *, score: float) -> dict[str, Any]:
    """Map a SQL chunk join row to a plain dict."""
    return {
        "id": int(r[0]),
        "chunk_id": r[1],
        "text": r[2],
        "heading_path": _heading_list(r[3]),
        "char_start": int(r[4]),
        "char_end": int(r[5]),
        "note_path": r[6],
        "note_title": r[7],
        "score": score,
    }
