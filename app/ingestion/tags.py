"""Open tag normalize/merge helpers for ingest and query filtering."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable

from app.agent import build_chat_model
from app.config import get_settings
from app.models import Profile, Provider
from app.monitoring import logger

# Keep document tag arrays small so GIN containment stays selective.
MAX_DOC_TAGS = 8
MAX_QUERY_TAGS = 3
_MAX_TAG_LEN = 40

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _aux_timeout_seconds() -> float:
    """Timeout budget for helper LLM calls used in metadata extraction."""
    return max(1.0, float(get_settings().llm_aux_timeout_seconds))


def normalize_tag(raw: str) -> str | None:
    """Lowercase/slug a freeform tag; return None when empty or junk."""
    text = (raw or "").strip().lower()
    if not text:
        return None
    text = text.lstrip("#")
    text = text.replace("_", "-").replace(" ", "-")
    text = _SLUG_RE.sub("-", text).strip("-")
    if not text or len(text) > _MAX_TAG_LEN:
        return None
    # Require at least one alphanumeric character.
    if not any(ch.isalnum() for ch in text):
        return None
    return text


def normalize_tags(tags: Iterable[str], *, limit: int | None = None) -> list[str]:
    """Normalize tags to a unique ordered list, optionally capped."""
    seen: set[str] = set()
    out: list[str] = []
    for item in tags:
        tag = normalize_tag(str(item))
        if tag is None or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
        if limit is not None and len(out) >= limit:
            break
    return out


def merge_tags(*lists: Iterable[str], limit: int = MAX_DOC_TAGS) -> list[str]:
    """Merge tag lists (earlier lists win); normalize and cap length."""
    combined: list[str] = []
    for group in lists:
        combined.extend(str(t) for t in group)
    return normalize_tags(combined, limit=limit)


def parse_tags_json(raw: str, *, limit: int) -> list[str]:
    """Extract a tags array from an LLM JSON object reply."""
    match = _JSON_OBJECT_RE.search(raw or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    tags = data.get("tags")
    if isinstance(tags, str):
        return normalize_tags([tags], limit=limit)
    if isinstance(tags, list):
        return normalize_tags((str(t) for t in tags), limit=limit)
    return []


async def suggest_document_tags(
    profile: Profile,
    *,
    title: str,
    body: str,
) -> list[str]:
    """Ask the rerank/evaluator model for topical vault tags for one note."""
    if not (profile.rerank_model or "").strip():
        return []
    model = build_chat_model(profile.rerank_model, Provider(profile.rerank_provider))
    excerpt = (body or "")[:2500]
    prompt = (
        "Propose topical tags for this personal Obsidian vault note. "
        "Return ONLY JSON: {\"tags\":[\"...\"]} with 3-8 short tags "
        "(lowercase words or kebab-case). No explanations.\n\n"
        f"Title: {title}\n\nNote:\n{excerpt}"
    )
    try:
        response = await asyncio.wait_for(
            model.ainvoke(prompt),
            timeout=_aux_timeout_seconds(),
        )
        raw = getattr(response, "content", "") or str(response)
        return parse_tags_json(raw, limit=MAX_DOC_TAGS)
    except Exception as exc:  # noqa: BLE001
        logger.info("Document tag imbuement skipped (%s)", exc)
        return []


async def extract_query_tags(question: str, profile: Profile) -> list[str]:
    """Ask the rerank/evaluator model for 1-3 filter tags from a question."""
    if not (question or "").strip() or not (profile.rerank_model or "").strip():
        return []
    model = build_chat_model(profile.rerank_model, Provider(profile.rerank_provider))
    prompt = (
        "Extract key topical tags to filter a personal Obsidian vault before search. "
        "Return ONLY JSON: {\"tags\":[\"...\"]} with 1-3 short tags, or {\"tags\":[]} "
        "if the question has no useful filter tags. Use lowercase/kebab-case.\n\n"
        f"Question: {question}"
    )
    try:
        response = await asyncio.wait_for(
            model.ainvoke(prompt),
            timeout=_aux_timeout_seconds(),
        )
        raw = getattr(response, "content", "") or str(response)
        return parse_tags_json(raw, limit=MAX_QUERY_TAGS)
    except Exception as exc:  # noqa: BLE001
        logger.info("Query tag extraction skipped (%s)", exc)
        return []
