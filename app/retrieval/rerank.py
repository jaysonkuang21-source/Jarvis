"""LLM relevance scoring / reranking of retrieved chunks."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.agent import build_chat_model
from app.config import get_settings
from app.models import Profile, Provider


def _aux_timeout_seconds() -> float:
    """Timeout budget for rerank-model helper calls."""
    return max(1.0, float(get_settings().llm_aux_timeout_seconds))


async def rerank_chunks(
    question: str,
    chunks: list[dict[str, Any]],
    profile: Profile,
    *,
    threshold: float = 0.35,
) -> list[dict[str, Any]]:
    """Ask the rerank model to score each chunk; drop below ``threshold``."""
    if not chunks:
        return []

    model = build_chat_model(profile.rerank_model, Provider(profile.rerank_provider))
    catalog = [
        {"i": i, "path": c.get("note_path", ""), "text": (c.get("text") or "")[:600]}
        for i, c in enumerate(chunks)
    ]
    prompt = (
        "Score each passage for relevance to the question from 0.0 to 1.0.\n"
        "Return ONLY a JSON array of objects: [{\"i\": 0, \"score\": 0.8}, ...]\n\n"
        f"Question: {question}\n\nPassages:\n{json.dumps(catalog, ensure_ascii=False)}"
    )
    try:
        response = await asyncio.wait_for(
            model.ainvoke(prompt),
            timeout=_aux_timeout_seconds(),
        )
        raw = getattr(response, "content", "") or str(response)
        scores = _parse_scores(raw)
    except Exception:  # noqa: BLE001
        return chunks

    scored: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks):
        score = scores.get(i, float(chunk.get("score") or 0.0))
        if score < threshold:
            continue
        row = dict(chunk)
        row["score"] = score
        scored.append(row)
    scored.sort(key=lambda c: float(c.get("score") or 0.0), reverse=True)
    return scored or chunks[: max(1, min(3, len(chunks)))]


async def grade_relevant(
    question: str,
    chunks: list[dict[str, Any]],
    profile: Profile,
) -> bool:
    """Return True when the grader thinks the set answers the question."""
    if not chunks:
        return False
    model = build_chat_model(profile.rerank_model, Provider(profile.rerank_provider))
    joined = "\n---\n".join((c.get("text") or "")[:400] for c in chunks[:8])
    prompt = (
        "Does the following context contain enough relevant information to answer "
        "the question? Reply with YES or NO only.\n\n"
        f"Question: {question}\n\nContext:\n{joined}"
    )
    try:
        response = await asyncio.wait_for(
            model.ainvoke(prompt),
            timeout=_aux_timeout_seconds(),
        )
        raw = (getattr(response, "content", "") or str(response)).strip().upper()
        return raw.startswith("YES")
    except Exception:  # noqa: BLE001
        return bool(chunks)


async def rewrite_query(question: str, profile: Profile) -> str:
    """Produce a rewritten search query for an agentic retry."""
    model = build_chat_model(profile.rerank_model, Provider(profile.rerank_provider))
    prompt = (
        "Rewrite the user question into a concise search query for a personal "
        "Obsidian vault. Return only the rewritten query.\n\n"
        f"Question: {question}"
    )
    try:
        response = await asyncio.wait_for(
            model.ainvoke(prompt),
            timeout=_aux_timeout_seconds(),
        )
        text = (getattr(response, "content", "") or str(response)).strip()
        return text or question
    except Exception:  # noqa: BLE001
        return question


def _parse_scores(raw: str) -> dict[int, float]:
    """Extract {index: score} from a model JSON reply."""
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    out: dict[int, float] = {}
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            out[int(item["i"])] = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
    return out
