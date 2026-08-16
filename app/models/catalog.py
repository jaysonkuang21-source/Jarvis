"""Curated model catalog loader with mtime-based in-process cache."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, get_settings

DEFAULT_CATALOG_PATH = PROJECT_ROOT / "config" / "model_catalog.json"

ROLE_KEYS = (
    "chat",
    "voice",
    "embedding",
    "chunk_decision",
    "extraction",
    "rerank",
)


@dataclass(frozen=True)
class CatalogEntry:
    """One curated model row from ``model_catalog.json``."""

    id: str
    hf_id: str | None = None
    parameter_b: float | None = None
    est_vram_mb: int | None = None
    size_bytes: int | None = None
    tier: str | None = None
    roles: dict[str, float] = field(default_factory=dict)
    notes: str = ""


@dataclass
class _Cache:
    """In-memory catalog cache keyed by path mtime."""

    path: Path | None = None
    mtime_ns: int = -1
    entries: dict[str, CatalogEntry] = field(default_factory=dict)
    loaded_at: float = 0.0


_cache = _Cache()


def catalog_path() -> Path:
    """Resolve the catalog JSON path from settings or the repo default."""
    settings = get_settings()
    configured = getattr(settings, "model_catalog_path", None)
    if isinstance(configured, Path):
        return configured
    return DEFAULT_CATALOG_PATH


def _parse_entry(model_id: str, raw: dict[str, Any]) -> CatalogEntry:
    """Build a CatalogEntry from one JSON object, coercing score keys."""
    roles_raw = raw.get("roles") or {}
    roles: dict[str, float] = {}
    for key in ROLE_KEYS:
        if key in roles_raw:
            try:
                roles[key] = float(roles_raw[key])
            except (TypeError, ValueError):
                continue
    parameter_b = raw.get("parameter_b")
    est_vram = raw.get("est_vram_mb")
    size_bytes = raw.get("size_bytes")
    raw_hf = raw.get("hf_id")
    hf_id: str | None = None
    if isinstance(raw_hf, str) and raw_hf.strip():
        # Lazy import avoids import cycles; drop unsafe ids so they cannot
        # become outbound URL material later.
        from app.models.hf_metrics import is_safe_hf_id

        candidate = raw_hf.strip()
        hf_id = candidate if is_safe_hf_id(candidate) else None
    return CatalogEntry(
        id=model_id,
        hf_id=hf_id,
        parameter_b=float(parameter_b) if parameter_b is not None else None,
        est_vram_mb=int(est_vram) if est_vram is not None else None,
        size_bytes=int(size_bytes) if size_bytes is not None else None,
        tier=raw.get("tier"),
        roles=roles,
        notes=str(raw.get("notes") or ""),
    )


def load_catalog(*, force: bool = False) -> dict[str, CatalogEntry]:
    """Load curated catalog entries, refreshing when the file mtime changes."""
    path = catalog_path()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        _cache.path = path
        _cache.mtime_ns = -1
        _cache.entries = {}
        _cache.loaded_at = time.monotonic()
        return {}

    if (
        not force
        and _cache.path == path
        and _cache.mtime_ns == mtime_ns
        and _cache.entries
    ):
        return _cache.entries

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _cache.path = path
        _cache.mtime_ns = mtime_ns
        _cache.entries = {}
        _cache.loaded_at = time.monotonic()
        return {}

    models = payload.get("models") if isinstance(payload, dict) else None
    entries: dict[str, CatalogEntry] = {}
    if isinstance(models, dict):
        for model_id, raw in models.items():
            if not isinstance(model_id, str) or not isinstance(raw, dict):
                continue
            entries[model_id] = _parse_entry(model_id, raw)

    _cache.path = path
    _cache.mtime_ns = mtime_ns
    _cache.entries = entries
    _cache.loaded_at = time.monotonic()
    return entries


def catalog_mtime_ns() -> int:
    """Return the cached catalog file mtime (0 if missing)."""
    load_catalog()
    return max(_cache.mtime_ns, 0)


def lookup_catalog(model_id: str) -> CatalogEntry | None:
    """Find a catalog row by exact id, then by Ollama base name before ``:``."""
    entries = load_catalog()
    if model_id in entries:
        return entries[model_id]
    base = model_id.split(":", 1)[0]
    if base in entries:
        return entries[base]
    return None


def clear_catalog_cache() -> None:
    """Drop the in-process catalog cache (tests)."""
    _cache.path = None
    _cache.mtime_ns = -1
    _cache.entries = {}
    _cache.loaded_at = 0.0
