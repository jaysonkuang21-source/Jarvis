"""Retrieval package public surface."""

from __future__ import annotations

__all__ = ["PostgresHybridEngine"]


def __getattr__(name: str):
    """Lazy-export the engine to avoid circular imports at package load."""
    if name == "PostgresHybridEngine":
        from app.retrieval.engine import PostgresHybridEngine as Engine

        return Engine
    raise AttributeError(name)
