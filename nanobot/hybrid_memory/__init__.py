"""hybrid_memory — SQLite + semantic vector memory for nanobot.

Public API is available at package level via lazy __getattr__ so that importing
the *package* never triggers the nanobot.agent circular-import chain.

Usage:
    from nanobot.hybrid_memory import HybridMemoryStore, SqliteManager
    # or directly from the sub-modules:
    from nanobot.hybrid_memory.stores import HybridMemoryStore
"""
from __future__ import annotations

__all__ = [
    "HybridMemoryStore",
    "HybridSessionManager",
    "SqliteManager",
    "get_account_db_path",
    "get_workspace_db_path",
    "ZvecManager",
    "get_account_zvec_path",
]

# Lazy loader — avoids the circular import that arises when this package is
# initialised before nanobot.agent is fully loaded (stores → agent.memory →
# agent.__init__ → loop → hybrid_memory.stores).
def __getattr__(name: str):  # noqa: N807  (module-level __getattr__)
    if name in ("HybridMemoryStore", "HybridSessionManager"):
        from nanobot.hybrid_memory import stores as _stores
        return getattr(_stores, name)
    if name in ("SqliteManager", "get_account_db_path", "get_workspace_db_path"):
        from nanobot.hybrid_memory import sqlite_manager as _sm
        return getattr(_sm, name)
    if name in ("ZvecManager", "get_account_zvec_path"):
        from nanobot.hybrid_memory import zvec_manager as _zv
        return getattr(_zv, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
