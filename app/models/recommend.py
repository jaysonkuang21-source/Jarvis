"""Role-aware model scorer and recommend API helpers.

Score (deterministic):
  0.45 * curated_role + 0.20 * availability + 0.20 * hf_signal + 0.15 * efficiency
"""

from __future__ import annotations

import math
from typing import Iterable

from app.models import (
    RECOMMEND_ROLES,
    IssueLevel,
    ModelInfo,
    ModelRecommendation,
    Profile,
    Provider,
    RecommendRequest,
    RecommendResponse,
    RoleRecommendation,
    SystemInfo,
    validate_profile,
)
from app.models.catalog import CatalogEntry, load_catalog, lookup_catalog
from app.models.hf_metrics import HfModelMetrics, fetch_many
from app.system.hardware import probe_system

WEIGHT_CURATED = 0.45
WEIGHT_AVAIL = 0.20
WEIGHT_HF = 0.20
WEIGHT_EFF = 0.15

ROLE_FIELD = {
    "chat": ("chat_model", "chat_provider"),
    "voice": ("voice_model", "voice_provider"),
    "embedding": ("embedding_model", "embedding_provider"),
    "chunk_decision": ("chunk_decision_model", "chunk_decision_provider"),
    "extraction": ("extraction_model", "extraction_provider"),
    "rerank": ("rerank_model", "rerank_provider"),
}


def normalize_roles(roles: list[str] | None) -> list[str]:
    """Return requested roles intersected with the supported set, or all roles."""
    if not roles:
        return list(RECOMMEND_ROLES)
    allowed = set(RECOMMEND_ROLES)
    out: list[str] = []
    for role in roles:
        if role in allowed and role not in out:
            out.append(role)
    return out or list(RECOMMEND_ROLES)


def candidates_for_role(role: str, models: dict[str, ModelInfo]) -> list[ModelInfo]:
    """Select registry models eligible for a profile role."""
    values = list(models.values())
    if role == "embedding":
        return [m for m in values if m.is_embedding]
    chatlike = [m for m in values if not m.is_embedding]
    if role == "extraction":
        # Prefer tool-capable chat models but keep the rest as lower-ranked options.
        with_tools = [m for m in chatlike if m.supports_tools]
        return with_tools or chatlike
    if role in ("voice", "chunk_decision", "rerank"):
        # Prefer tiny/small tiers when known; fall back to all non-embed.
        preferred = [
            m
            for m in chatlike
            if (m.tier or "").lower() in ("tiny", "small") or (m.parameter_b or 99) <= 4
        ]
        return preferred or chatlike
    return chatlike


def memory_budget_mb(system: SystemInfo) -> tuple[int | None, bool]:
    """Return (budget_mb, using_gpu) for the fit gate."""
    free_vram = [
        g.vram_free_mb
        for g in system.gpus
        if g.vram_free_mb is not None
    ]
    if free_vram:
        return max(free_vram), True
    total_vram = [
        g.vram_total_mb
        for g in system.gpus
        if g.vram_total_mb is not None
    ]
    if total_vram:
        return max(total_vram), True
    return system.ram_available_mb, False


def model_fits(info: ModelInfo, system: SystemInfo) -> bool:
    """True when the model should run on this machine (cloud OpenAI always fits)."""
    if info.provider is Provider.OPENAI:
        return bool(info.available)
    est = info.est_vram_mb
    if est is None:
        entry = lookup_catalog(info.id)
        est = entry.est_vram_mb if entry else None
    if est is None:
        return True  # unknown footprint — do not hard-block
    budget, using_gpu = memory_budget_mb(system)
    if budget is None:
        return True
    need = est if using_gpu else int(est * 1.2)
    return need <= budget


def availability_score(info: ModelInfo) -> tuple[float, list[str]]:
    """Score availability: ready / pull-needed / blocked."""
    reasons: list[str] = []
    if info.available:
        reasons.append("ready on this machine")
        return 1.0, reasons
    reason = (info.unavailable_reason or "").lower()
    if "pull" in reason or "ollama" in reason:
        reasons.append("needs ollama pull")
        return 0.4, reasons
    if "api key" in reason or "openai" in reason:
        reasons.append("needs API key")
        return 0.0, reasons
    reasons.append(info.unavailable_reason or "unavailable")
    return 0.0, reasons


def hf_signal(metrics: HfModelMetrics | None, *, online: bool) -> tuple[float, list[str]]:
    """Log-scaled Hub popularity when online cache hit; else neutral 0.5."""
    if not online or metrics is None:
        return 0.5, []
    downloads = metrics.downloads or 0
    likes = metrics.likes or 0
    # log10(1 + d + 10*likes) scaled softly into 0..1
    raw = math.log10(1.0 + downloads + 10.0 * likes)
    score = max(0.0, min(1.0, raw / 7.0))
    reasons = [f"HF downloads={downloads:,} likes={likes:,}"]
    return score, reasons


def efficiency_score(
    info: ModelInfo,
    *,
    curated_role: float,
    system: SystemInfo,
) -> tuple[float, list[str]]:
    """Reward quality-per-VRAM and penalize oversized local models."""
    reasons: list[str] = []
    if info.provider is Provider.OPENAI:
        reasons.append("cloud (no local VRAM)")
        return 0.85, reasons
    est = info.est_vram_mb or 0
    budget, using_gpu = memory_budget_mb(system)
    quality = curated_role / 100.0
    if est <= 0:
        return 0.6, reasons
    # Higher quality at lower VRAM is better.
    density = quality / math.log10(10.0 + est)
    density = max(0.0, min(1.0, density * 2.2))
    if budget is not None and est > 0:
        need = est if using_gpu else int(est * 1.2)
        if need > budget:
            reasons.append("likely oversized for free memory")
            return density * 0.25, reasons
        headroom = budget / need
        if headroom >= 2.0:
            reasons.append("comfortable memory headroom")
            density = min(1.0, density + 0.1)
    return density, reasons


def curated_role_score(info: ModelInfo, role: str) -> float:
    """0..100 role score from ModelInfo or catalog fallback."""
    if info.role_scores and role in info.role_scores:
        return float(info.role_scores[role])
    entry = lookup_catalog(info.id)
    if entry and role in entry.roles:
        return float(entry.roles[role])
    return 40.0 if not info.is_embedding else (80.0 if role == "embedding" else 0.0)


def score_candidate(
    info: ModelInfo,
    role: str,
    system: SystemInfo,
    *,
    online: bool,
    metrics: HfModelMetrics | None,
    metrics_degraded: bool,
    profile: Profile,
    models: dict[str, ModelInfo],
) -> ModelRecommendation:
    """Compute weighted score and reasons for one model/role pair."""
    curated = curated_role_score(info, role)
    avail, avail_reasons = availability_score(info)
    hf, hf_reasons = hf_signal(metrics, online=online)
    eff, eff_reasons = efficiency_score(info, curated_role=curated, system=system)
    total = (
        WEIGHT_CURATED * (curated / 100.0)
        + WEIGHT_AVAIL * avail
        + WEIGHT_HF * hf
        + WEIGHT_EFF * eff
    )
    fits = model_fits(info, system)
    needs_pull = (not info.available) and "pull" in (info.unavailable_reason or "").lower()

    reasons = [
        f"catalog role {curated:.0f}/100",
        *avail_reasons,
        *hf_reasons,
        *eff_reasons,
    ]
    if not fits:
        reasons.append("may not fit free RAM/VRAM")

    disabled_reason = ""
    model_field, provider_field = ROLE_FIELD[role]
    candidate = profile.model_copy(
        update={model_field: info.id, provider_field: info.provider}
    )
    validation = validate_profile(candidate, models)
    blocking = [
        i
        for i in validation.issues
        if i.field == model_field and i.level is IssueLevel.ERROR
    ]
    if blocking:
        disabled_reason = blocking[0].message
        reasons.append(f"profile rule: {disabled_reason}")
        total *= 0.2

    return ModelRecommendation(
        id=info.id,
        provider=info.provider,
        score=round(total, 4),
        reasons=reasons,
        fits=fits and not disabled_reason,
        needs_pull=needs_pull,
        metrics_degraded=metrics_degraded and online,
        available=info.available,
        disabled_reason=disabled_reason,
    )


def enrich_model_from_catalog(info: ModelInfo, entry: CatalogEntry | None) -> ModelInfo:
    """Copy catalog metadata onto a ModelInfo when fields are still empty."""
    if entry is None:
        return info
    updates: dict = {}
    if info.parameter_b is None and entry.parameter_b is not None:
        updates["parameter_b"] = entry.parameter_b
    if info.est_vram_mb is None and entry.est_vram_mb is not None:
        updates["est_vram_mb"] = entry.est_vram_mb
    if info.size_bytes is None and entry.size_bytes is not None:
        updates["size_bytes"] = entry.size_bytes
    if info.hf_id is None and entry.hf_id is not None:
        updates["hf_id"] = entry.hf_id
    if info.role_scores is None and entry.roles:
        updates["role_scores"] = dict(entry.roles)
    if info.tier is None and entry.tier is not None:
        updates["tier"] = entry.tier
    return info.model_copy(update=updates) if updates else info


def merge_catalog_into_registry(models: dict[str, ModelInfo]) -> dict[str, ModelInfo]:
    """Apply curated catalog fields onto every registry entry (id + base name)."""
    catalog = load_catalog()
    merged: dict[str, ModelInfo] = {}
    for model_id, info in models.items():
        entry = catalog.get(model_id) or catalog.get(model_id.split(":", 1)[0])
        merged[model_id] = enrich_model_from_catalog(info, entry)
    # Ensure catalog-only Ollama stubs that were not discovered still appear
    # when the registry already listed them via curated lists.
    return merged


async def recommend_models(
    request: RecommendRequest,
    *,
    models: dict[str, ModelInfo],
    profile: Profile,
    system: SystemInfo | None = None,
) -> RecommendResponse:
    """Rank models per role using catalog, hardware, and optional HF metrics."""
    system = system or probe_system()
    roles = normalize_roles(request.roles)
    online = (
        request.online
        if request.online is not None
        else bool(profile.model_metrics_online)
    )
    models = merge_catalog_into_registry(models)

    hf_by_id: dict[str, HfModelMetrics] = {}
    metrics_degraded = False
    if online:
        hf_ids: list[str] = []
        for role in roles:
            for info in candidates_for_role(role, models):
                if info.hf_id and info.hf_id not in hf_ids:
                    hf_ids.append(info.hf_id)
        hf_by_id, metrics_degraded = await fetch_many(hf_ids)

    role_results: list[RoleRecommendation] = []
    any_degraded = metrics_degraded
    for role in roles:
        scored: list[ModelRecommendation] = []
        for info in candidates_for_role(role, models):
            metrics = hf_by_id.get(info.hf_id) if info.hf_id else None
            # If we asked online but this id has no metrics, treat as soft degrade.
            degraded = bool(online and info.hf_id and metrics is None) or metrics_degraded
            if degraded and info.hf_id:
                any_degraded = True
            scored.append(
                score_candidate(
                    info,
                    role,
                    system,
                    online=online,
                    metrics=metrics,
                    metrics_degraded=degraded,
                    profile=profile,
                    models=models,
                )
            )
        scored.sort(key=lambda r: (-r.score, r.id))
        top = scored[: request.top_n]
        role_degraded = any(r.metrics_degraded for r in top)
        role_results.append(
            RoleRecommendation(
                role=role,
                recommendations=top,
                metrics_degraded=role_degraded,
            )
        )

    return RecommendResponse(
        roles=role_results,
        system=system,
        online=online,
        metrics_degraded=any_degraded and online,
    )


def ensure_catalog_stub_models(models: dict[str, ModelInfo]) -> Iterable[ModelInfo]:
    """Yield catalog-backed ModelInfo rows missing from discovery (unused helper)."""
    for entry in load_catalog().values():
        if entry.id not in models:
            yield ModelInfo(
                id=entry.id,
                provider=Provider.OLLAMA,
                label=entry.id,
                is_embedding="embedding" in entry.roles and entry.roles.get("embedding", 0) > 0
                and entry.roles.get("chat", 0) == 0,
                parameter_b=entry.parameter_b,
                est_vram_mb=entry.est_vram_mb,
                size_bytes=entry.size_bytes,
                hf_id=entry.hf_id,
                role_scores=dict(entry.roles),
                tier=entry.tier,
                available=False,
                unavailable_reason=f"Run: ollama pull {entry.id}",
            )
