"""Vault walk, extract, embed, and community build for reindex."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.agent import build_chat_model
from app.db import database_configured, repo, try_ensure_schema
from app.ingestion import chunk_note, resolve_chunk_plan
from app.ingestion.chunkers import EvidenceChunk, extract_wikilinks
from app.ingestion.effort import ChunkPlan
from app.ingestion.embeddings import build_embeddings, embed_documents
from app.ingestion.prepare import PreparedNote, prepare_note
from app.ingestion.tags import merge_tags, suggest_document_tags
from app.models import Chunker, Profile, Provider
from app.monitoring import logger
from app.security import get_policy_engine

_WIKILINK_TARGET = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


async def run_reindex(profile: Profile, *, engine: Any | None = None) -> None:
    """Index the configured vault into Postgres (blocking work in threads as needed)."""
    if not database_configured() or not try_ensure_schema():
        raise RuntimeError("Postgres is not configured or unreachable")

    from app.cache import clear_answer_caches
    from app.ingestion.dim_guard import (
        clear_measured_embedding_cache,
        ensure_vector_schema_for_reindex,
    )

    # Fresh probes for this build — never trust a prior process cache entry.
    clear_measured_embedding_cache()

    policy = get_policy_engine()
    vault = policy.vault_path
    if vault is None or not vault.exists():
        raise RuntimeError("No vault configured. Set vault_path in config/rules.md.")

    if engine is not None:
        engine.set_indexing(True)
    else:
        await asyncio.to_thread(repo.set_indexing, True)

    try:
        measured_dims = await ensure_vector_schema_for_reindex(profile)

        notes = await asyncio.to_thread(_list_notes, vault)
        live_paths = {
            str(path.relative_to(vault)).replace("\\", "/") for path in notes
        }
        for path in notes:
            await _index_note(vault, path, profile)

        from app.ingestion.remove import prune_documents_missing_from_vault

        pruned = await asyncio.to_thread(
            prune_documents_missing_from_vault, vault, live_paths
        )
        if pruned:
            logger.info("Pruned %s document(s) missing from vault", pruned)

        await _build_communities(profile)

        dims = measured_dims
        try:
            from app.agent import get_registry

            models = await get_registry().all()
            info = models.get(profile.embedding_model)
            if info and info.dimensions and info.dimensions != measured_dims:
                logger.warning(
                    "Registry reports dims=%s but probe measured %s; using measured",
                    info.dimensions,
                    measured_dims,
                )
        except Exception:  # noqa: BLE001
            pass

        await asyncio.to_thread(
            lambda: repo.mark_ready(
                embedding_model=profile.embedding_model,
                extraction_model=profile.extraction_model,
                dims=dims,
            )
        )
        clear_answer_caches()
        logger.info("Reindex complete: %s notes (dims=%s)", len(notes), dims)
    finally:
        if engine is not None:
            engine.set_indexing(False)
        else:
            await asyncio.to_thread(repo.set_indexing, False)


def _list_notes(vault: Path) -> list[Path]:
    """Return markdown notes under the vault, skipping hidden folders."""
    notes: list[Path] = []
    for path in vault.rglob("*.md"):
        rel_parts = path.relative_to(vault).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        notes.append(path)
    return notes


async def _index_note(vault: Path, path: Path, profile: Profile) -> None:
    """Chunk, embed, extract, and upsert one note when content changed."""
    rel = str(path.relative_to(vault)).replace("\\", "/")
    text = path.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = repo.document_by_hash(rel)
    plan = resolve_chunk_plan(profile)

    if existing and existing[1] == digest:
        # Content unchanged: still refresh LLM document tags for non-recursive
        # chunkers so reindex picks up tag imbuement / rerank model changes.
        if plan.chunker is Chunker.RECURSIVE:
            return
        prepared = prepare_note(vault, path, text)
        title = prepared.title or path.stem
        tags = await _imbue_document_tags(profile, title, prepared, plan=plan)
        repo.upsert_document(
            rel, title, path.stat().st_mtime, digest, tags=tags
        )
        return

    prepared = prepare_note(vault, path, text)
    title = prepared.title or path.stem
    tags = await _imbue_document_tags(profile, title, prepared, plan=plan)
    doc_id = repo.upsert_document(
        rel, title, path.stat().st_mtime, digest, tags=tags
    )
    repo.delete_document_chunks(doc_id)

    # Semantic chunking needs the same embedder used at upsert time.
    embedder = build_embeddings(profile.embedding_model, profile.embedding_provider)
    _plan, chunks = chunk_note(
        prepared.working,
        profile,
        title=title,
        document_id=rel,
        embeddings=embedder,
        tags=tags,
    )
    if not chunks:
        return

    # Remap offsets onto the on-disk note for citations / parent expand.
    mapped: list[EvidenceChunk] = []
    for chunk in chunks:
        orig_start, orig_end = prepared.map_span(chunk.char_start, chunk.char_end)
        mapped.append(
            chunk.model_copy(update={"char_start": orig_start, "char_end": orig_end})
        )
    chunks = mapped

    embeddings = await embed_documents(profile, [c.text for c in chunks])
    chunk_db_ids: list[int] = []
    for chunk, emb in zip(chunks, embeddings, strict=False):
        cid = repo.insert_chunk(
            document_id=doc_id,
            chunk_id=chunk.chunk_id,
            text=chunk.text,
            heading_path=chunk.heading_path,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            embedding=emb,
        )
        chunk_db_ids.append(cid)

        # Wikilink edges.
        for raw in extract_wikilinks(chunk.text):
            m = _WIKILINK_TARGET.search(raw)
            if not m:
                continue
            target = m.group(1).strip()
            if not target:
                continue
            src = repo.upsert_entity(title)
            dst = repo.upsert_entity(target)
            repo.upsert_relationship(src, dst, "wikilink", "wikilink", [cid])
            repo.link_chunk_entity(cid, src)
            repo.link_chunk_entity(cid, dst)

    # LLM extraction (batched lightly per note).
    await _extract_entities(profile, title, chunks, chunk_db_ids)


async def _imbue_document_tags(
    profile: Profile,
    title: str,
    prepared: PreparedNote,
    *,
    plan: ChunkPlan | None = None,
) -> list[str]:
    """Merge frontmatter tags with evaluator-model suggestions for the note.

    Recursive chunking keeps frontmatter tags only; all other chunkers call
    ``rerank_model`` on the note body for topical metadata.
    """
    plan = plan or resolve_chunk_plan(profile)
    if plan.chunker is Chunker.RECURSIVE:
        return merge_tags(prepared.tags)

    body = (
        prepared.original[prepared.body_offset :]
        if prepared.body_offset
        else prepared.working
    ).strip()
    suggested = await suggest_document_tags(
        profile,
        title=title,
        body=body,
    )
    return merge_tags(prepared.tags, suggested)


async def _extract_entities(
    profile: Profile,
    note_title: str,
    chunks: list[EvidenceChunk],
    chunk_db_ids: list[int],
) -> None:
    """Call the extraction model for entities/relations on note chunks."""
    if not profile.extraction_model or not chunks:
        return

    model = build_chat_model(
        profile.extraction_model, Provider(profile.extraction_provider)
    )
    # Cap extraction calls for very large notes.
    pairs = list(zip(chunks, chunk_db_ids, strict=False))[:12]
    for chunk, cid in pairs:
        prompt = (
            "Extract entities and relationships from this vault note chunk. "
            "Return ONLY JSON: "
            '{"entities":[{"name":"...","description":"..."}],'
            '"relationships":[{"src":"...","dst":"...","type":"..."}]}\n\n'
            f"Note: {note_title}\n\nChunk:\n{chunk.text[:2000]}"
        )
        try:
            response = await model.ainvoke(prompt)
            raw = getattr(response, "content", "") or str(response)
            data = _parse_json_object(raw)
        except Exception as exc:  # noqa: BLE001
            logger.info("Extraction skipped for chunk %s (%s)", cid, exc)
            continue

        id_by_name: dict[str, int] = {}
        for ent in data.get("entities") or []:
            if not isinstance(ent, dict):
                continue
            name = str(ent.get("name") or "").strip()
            if not name:
                continue
            eid = repo.upsert_entity(name, str(ent.get("description") or ""))
            id_by_name[name.lower()] = eid
            repo.link_chunk_entity(cid, eid)

        for rel in data.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            src_n = str(rel.get("src") or "").strip()
            dst_n = str(rel.get("dst") or "").strip()
            if not src_n or not dst_n:
                continue
            src = id_by_name.get(src_n.lower()) or repo.upsert_entity(src_n)
            dst = id_by_name.get(dst_n.lower()) or repo.upsert_entity(dst_n)
            repo.upsert_relationship(
                src, dst, str(rel.get("type") or "related"), "llm", [cid]
            )


async def _build_communities(profile: Profile) -> None:
    """Connected-component communities with optional LLM summaries."""
    repo.clear_communities()
    entities = repo.list_entities()
    if not entities:
        return

    edges = repo.list_relationship_pairs()
    adj: dict[int, set[int]] = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    for eid, _ in entities:
        adj[eid]  # ensure node exists

    seen: set[int] = set()
    components: list[list[int]] = []
    for eid, _ in entities:
        if eid in seen:
            continue
        stack = [eid]
        comp: list[int] = []
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            comp.append(n)
            stack.extend(adj[n] - seen)
        if comp:
            components.append(comp)

    name_by_id = dict(entities)
    model = build_chat_model(profile.extraction_model, Provider(profile.extraction_provider))

    for i, members in enumerate(components):
        if len(members) < 1:
            continue
        label = name_by_id.get(members[0], f"community-{i}")
        names = [name_by_id.get(m, str(m)) for m in members[:20]]
        summary = f"Community around {label}: " + ", ".join(names)
        try:
            response = await model.ainvoke(
                "Write a 2-3 sentence summary of this entity community in a "
                f"personal knowledge vault.\nEntities: {', '.join(names)}"
            )
            text = (getattr(response, "content", "") or "").strip()
            if text:
                summary = text
        except Exception:  # noqa: BLE001
            pass
        level = 0 if len(members) > 8 else min(profile.community_level, 2)
        repo.create_community(level, label, members, summary)


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Best-effort JSON object parse from an LLM reply."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
