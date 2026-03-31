"""hybrid_memory — PostgreSQL + pgvector memory for nanobot.

Public API is available at package level via lazy __getattr__ so that importing
the *package* never triggers the nanobot.agent circular-import chain.

Usage:
    from nanobot.hybrid_memory import HybridMemoryStore, HybridSessionManager
    # or directly from the sub-modules:
    from nanobot.hybrid_memory.stores import HybridMemoryStore
"""
from __future__ import annotations

__all__ = [
    "HybridMemoryStore",
    "HybridSessionManager",
]

# Lazy loader — avoids the circular import that arises when this package is
# initialised before nanobot.agent is fully loaded (stores → agent.__init__ →
# loop → hybrid_memory.stores).
def __getattr__(name: str):  # noqa: N807  (module-level __getattr__)
    if name in ("HybridMemoryStore", "HybridSessionManager"):
        from nanobot.hybrid_memory import stores as _stores
        return getattr(_stores, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
