"""Embedding model/dim compatibility against the live Postgres vector columns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.db import database_configured, repo, try_ensure_schema
from app.ingestion.embeddings import embed_query
from app.models import Profile
from app.monitoring import logger


@dataclass(frozen=True, slots=True)
class EmbeddingCompatibility:
    """Result of comparing profile model, measured vectors, and column width."""

    ok: bool
    measured_dims: int | None
    column_dims: int | None
    meta_model: str | None
    meta_dims: int | None
    profile_model: str
    error: str | None = None
    # SSE/UI: index_not_ready | embedding_mismatch | postgres_unavailable | …
    code: str | None = None


_measured_cache: dict[str, int] = {}


def clear_measured_embedding_cache() -> None:
    """Drop cached probe dimensions so rebuild/reindex cannot false-pass checks."""
    _measured_cache.clear()


async def probe_embedding_dims(profile: Profile) -> int:
    """Embed a short probe string and return the measured vector length.

    Results are cached per process for ``provider:model`` so chat traffic does
    not re-probe on every turn.
    """
    key = f"{profile.embedding_provider}:{profile.embedding_model}"
    cached = _measured_cache.get(key)
    if cached is not None:
        return cached
    vector = await embed_query(profile, "jarvis embedding dimension probe")
    measured = len(vector)
    _measured_cache[key] = measured
    return measured


async def check_embedding_compatibility(profile: Profile) -> EmbeddingCompatibility:
    """Compare profile model + measured dims + catalog column width + meta.

    Meta alone is not authoritative — a column created at the wrong width must
    fail readiness even if index_meta looks fine.
    """
    profile_model = profile.embedding_model
    schema_ok = await asyncio.to_thread(
        lambda: database_configured() and try_ensure_schema()
    )
    if not schema_ok:
        return EmbeddingCompatibility(
            ok=False,
            measured_dims=None,
            column_dims=None,
            meta_model=None,
            meta_dims=None,
            profile_model=profile_model,
            error="Postgres is not configured or unreachable.",
            code="postgres_unavailable",
        )

    meta = await asyncio.to_thread(repo.fetch_index_meta)
    column_dims = await asyncio.to_thread(repo.measure_vector_column_dims, "chunks")
    try:
        measured = await probe_embedding_dims(profile)
    except Exception as exc:  # noqa: BLE001
        return EmbeddingCompatibility(
            ok=False,
            measured_dims=None,
            column_dims=column_dims,
            meta_model=meta.get("embedding_model"),
            meta_dims=meta.get("embedding_dims"),
            profile_model=profile_model,
            error=f"Could not measure embedding dimensions: {exc}",
            code="embedding_probe_failed",
        )

    meta_model = meta.get("embedding_model")
    meta_dims = meta.get("embedding_dims")
    mismatch_problems: list[str] = []
    ready = bool(meta.get("ready"))

    if meta_model and meta_model != profile_model:
        mismatch_problems.append(
            f"index was built with embedding model {meta_model!r}, "
            f"profile asks for {profile_model!r}"
        )
    if meta_dims is not None and int(meta_dims) != measured:
        mismatch_problems.append(
            f"index_meta.embedding_dims={meta_dims} but live model emits {measured}"
        )
    if column_dims is not None and column_dims != measured:
        mismatch_problems.append(
            f"chunks.embedding is vector({column_dims}) but live model emits {measured}"
        )

    if mismatch_problems:
        detail = "; ".join(mismatch_problems)
        if not ready:
            detail = f"{detail}; index is not marked ready"
        error = (
            f"Embedding mismatch — vault vectors do not match the selected model. "
            f"{detail}. Reindex the vault to rebuild vector columns."
        )
        return EmbeddingCompatibility(
            ok=False,
            measured_dims=measured,
            column_dims=column_dims,
            meta_model=meta_model,
            meta_dims=int(meta_dims) if meta_dims is not None else None,
            profile_model=profile_model,
            error=error,
            code="embedding_mismatch",
        )

    if not ready:
        return EmbeddingCompatibility(
            ok=False,
            measured_dims=measured,
            column_dims=column_dims,
            meta_model=meta_model,
            meta_dims=int(meta_dims) if meta_dims is not None else None,
            profile_model=profile_model,
            error=(
                "Vault index is not ready for queries yet. "
                "Run Reindex to build embeddings, then try again."
            ),
            code="index_not_ready",
        )

    return EmbeddingCompatibility(
        ok=True,
        measured_dims=measured,
        column_dims=column_dims,
        meta_model=meta_model,
        meta_dims=int(meta_dims) if meta_dims is not None else None,
        profile_model=profile_model,
        error=None,
        code=None,
    )


async def ensure_vector_schema_for_reindex(profile: Profile) -> int:
    """Probe dims; destructively rebuild all vector tables on model/dim mismatch.

    Returns the measured width to store in index_meta after a successful build.
    """
    from app.cache import clear_answer_caches
    from app.db import rebuild_vector_schema

    # Stale probe dims must not false-pass compatibility after a prior model.
    clear_measured_embedding_cache()

    if not database_configured() or not try_ensure_schema():
        raise RuntimeError("Postgres is not configured or unreachable")

    measured = await probe_embedding_dims(profile)
    meta = repo.fetch_index_meta()
    column_dims = repo.measure_vector_column_dims("chunks")
    meta_model = meta.get("embedding_model")
    meta_dims = meta.get("embedding_dims")

    reasons: list[str] = []
    if column_dims is not None and column_dims != measured:
        reasons.append(f"column vector({column_dims}) vs measured {measured}")
    if meta_dims is not None and int(meta_dims) != measured:
        reasons.append(f"meta dims {meta_dims} vs measured {measured}")
    if meta_model and meta_model != profile.embedding_model:
        reasons.append(
            f"meta model {meta_model!r} vs profile {profile.embedding_model!r}"
        )

    if reasons:
        logger.warning(
            "Confirmed embedding mismatch (%s); rebuilding vector schema at dims=%s",
            "; ".join(reasons),
            measured,
        )
        rebuild_vector_schema(measured)
        clear_answer_caches()
        repo.mark_not_ready()

    return measured
