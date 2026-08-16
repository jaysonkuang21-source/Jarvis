"""Postgres hybrid RetrievalEngine (LangGraph-orchestrated query path)."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.db import database_configured, repo, try_ensure_schema
from app.ingestion.chunkers import estimate_tokens
from app.models import (
    ChatMessage,
    DoneEvent,
    ErrorEvent,
    IndexStatus,
    Profile,
    StreamEvent,
)
from app.monitoring import get_metrics, logger
from app.monitoring import tracing
from app.retrieval.expand import chunk_to_citation, expand_chunks
from app.retrieval.graph import prompt_token_count, stream_query


# Re-export helpers used by tests / other modules.
__all__ = [
    "PostgresHybridEngine",
    "chunk_to_citation",
    "expand_chunks",
]


class PostgresHybridEngine:
    """Hybrid vector + FTS + graph retrieval backed by Postgres."""

    name = "postgres-hybrid"

    def __init__(self) -> None:
        """No I/O until query/index_status; schema is ensured lazily."""
        self._indexing = False

    def _sync_index_status(self) -> IndexStatus:
        """Blocking vault walk + Postgres meta/counts for index readiness."""
        from app.security import get_policy_engine

        policy = get_policy_engine()
        vault = policy.vault_path
        total = 0
        if vault and vault.exists():
            total = sum(
                1
                for p in vault.rglob("*.md")
                if not any(part.startswith(".") for part in p.relative_to(vault).parts)
            )

        if not database_configured() or not try_ensure_schema():
            return IndexStatus(
                engine=self.name,
                ready=False,
                indexing=False,
                total_notes=total,
                indexed_notes=0,
            )

        meta = repo.fetch_index_meta()
        # Readiness needs meta.ready and agreement between meta dims and the
        # live vector column width (meta alone is not enough).
        column_dims = None
        try:
            column_dims = repo.measure_vector_column_dims("chunks")
        except Exception:  # noqa: BLE001
            column_dims = None

        ready = bool(meta.get("ready"))
        if ready and column_dims is not None and meta.get("embedding_dims") is not None:
            if int(meta["embedding_dims"]) != int(column_dims):
                ready = False
                try:
                    repo.mark_not_ready()
                except Exception:  # noqa: BLE001
                    pass

        db_indexing = bool(meta.get("indexing"))
        return IndexStatus(
            engine=self.name,
            ready=ready,
            indexing=db_indexing or self._indexing,
            indexing_stale=db_indexing and not self._indexing,
            total_notes=total,
            indexed_notes=repo.count_table("documents"),
            entities=repo.count_table("entities"),
            relationships=repo.count_table("relationships"),
            communities=repo.count_table("communities"),
            embedding_model=meta.get("embedding_model"),
            extraction_model=meta.get("extraction_model"),
            last_indexed_at=meta.get("last_indexed_at"),
        )

    async def index_status(self) -> IndexStatus:
        """Readiness and vault/index stats from Postgres when available."""
        return await asyncio.to_thread(self._sync_index_status)

    def set_indexing(self, flag: bool) -> None:
        """Track an in-process reindex job."""
        self._indexing = flag
        if database_configured():
            try:
                repo.set_indexing(flag)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not persist indexing flag (%s)", exc)

    async def query(
        self,
        question: str,
        profile: Profile,
        history: list[ChatMessage] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run LangGraph retrieval/generation; DB and dim guards stay outside."""
        history = history or []
        started = time.monotonic()
        message_id = uuid.uuid4().hex

        schema_ok = await asyncio.to_thread(
            lambda: database_configured() and try_ensure_schema()
        )
        if not schema_ok:
            yield ErrorEvent(
                message="Postgres is not configured or unreachable. "
                "Set JARVIS_DATABASE_URL and ensure pgvector is installed."
            )
            yield DoneEvent(
                message_id=message_id,
                cancelled=False,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return

        from app.ingestion.dim_guard import check_embedding_compatibility

        compat = await check_embedding_compatibility(profile)
        if not compat.ok:
            if compat.code == "embedding_mismatch":
                try:
                    await asyncio.to_thread(repo.mark_not_ready)
                except Exception:  # noqa: BLE001
                    pass
            yield ErrorEvent(
                message=compat.error
                or "Embedding model/dimension mismatch. Reindex required.",
                code=compat.code or "embedding_mismatch",
                recoverable=False,
            )
            yield DoneEvent(
                message_id=message_id,
                cancelled=False,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            return

        metrics_out: dict[str, Any] = {}
        try:
            with tracing(profile.tracing_enabled):
                async for event in stream_query(
                    question, profile, history, metrics_out=metrics_out
                ):
                    yield event
        except Exception:  # noqa: BLE001
            logger.exception("LangGraph retrieval workflow failed")
            yield ErrorEvent(
                message="Retrieval failed. Check the index is ready and try again.",
                code="retrieval_failed",
            )

        assembled = metrics_out.get("assembled") or ""
        messages = metrics_out.get("prompt_messages") or []
        if assembled or messages:
            get_metrics().record_tokens(
                input_tokens=prompt_token_count(messages) if messages else 0,
                output_tokens=estimate_tokens(assembled),
            )

        yield DoneEvent(
            message_id=message_id,
            cancelled=False,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
