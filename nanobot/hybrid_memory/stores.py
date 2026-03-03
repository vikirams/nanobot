from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from nanobot.session.manager import Session
from nanobot.hybrid_memory.sqlite_manager import SqliteManager
from nanobot.hybrid_memory.zvec_manager import ZvecManager
from nanobot.providers.base import LLMProvider


class HybridSessionManager:
    """
    Manages conversation sessions using SQLite for persistence.

    Accepts a shared SqliteManager so the caller controls connection lifetime
    and avoids opening two connections to the same database file.
    Session cache uses LRU eviction to bound memory (default max 500 entries).
    """

    _CACHE_MAX_SIZE = 500

    def __init__(self, workspace: Path, sqlite_manager: SqliteManager):
        self.workspace = workspace
        self._sqlite_manager = sqlite_manager
        self._cache: OrderedDict[str, Session] = OrderedDict()

    async def get_or_create(self, key: str) -> Session:
        """Get an existing session from SQLite or create a new one."""
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        session = await self._load_session_from_db(key)
        if session is None:
            logger.info(f"Creating new session for key: {key}")
            session = Session(key=key)

        while len(self._cache) >= self._CACHE_MAX_SIZE:
            self._cache.popitem(last=False)
        self._cache[key] = session
        return session

    async def _load_session_from_db(self, key: str) -> Optional[Session]:
        """Load a session and reconstruct its full message list from SQLite.

        Also restores persisted metadata (last_consolidated, workspace_key) so
        memory consolidation picks up exactly where it left off after a restart.
        """
        # get_messages_for_session internally calls _get_conn() — no need for a
        # separate, direct call to the private method.
        messages_data = await self._sqlite_manager.get_messages_for_session(key, limit=-1)

        if not messages_data:
            return None

        messages = []
        for msg_row in messages_data:
            # Prefer raw_data (preserves tool_calls, tool_call_id, name, etc.)
            if msg_row.get("raw_data"):
                msg = dict(msg_row["raw_data"])
            else:
                # Fallback for rows written before raw_data was added
                msg = {"role": msg_row["role"], "content": msg_row["content"]}
                if msg_row.get("presented_data_context"):
                    msg["presented_data_context"] = msg_row["presented_data_context"]

            # Ensure timestamp is present
            msg.setdefault("timestamp", msg_row.get("timestamp", datetime.now().isoformat()))
            messages.append(msg)

        created_at = datetime.fromisoformat(messages[0]["timestamp"])
        updated_at = datetime.fromisoformat(messages[-1]["timestamp"])

        # Restore persisted metadata — without this last_consolidated is always 0
        # on restart, forcing re-consolidation of the entire history.
        saved_meta = await self._sqlite_manager.get_session_metadata(key)
        last_consolidated = saved_meta["last_consolidated"] if saved_meta else 0
        workspace_key = saved_meta["workspace_key"] if saved_meta else "__workspace__"
        extra_metadata = saved_meta["extra_metadata"] if saved_meta else {}

        logger.info(f"Loaded {len(messages)} messages for session: {key} (last_consolidated={last_consolidated})")
        return Session(
            key=key,
            messages=messages,
            created_at=created_at,
            updated_at=updated_at,
            last_consolidated=last_consolidated,
            metadata={"workspace_key": workspace_key, **extra_metadata},
        )

    async def save(self, session: Session) -> None:
        """Persist session metadata to SQLite and update in-memory cache.

        Individual messages are written by add_message() on every turn.
        This method persists last_consolidated and workspace_key so that a
        process restart can resume consolidation from the correct point.
        """
        if session.key in self._cache:
            self._cache.move_to_end(session.key)
        else:
            while len(self._cache) >= self._CACHE_MAX_SIZE:
                self._cache.popitem(last=False)
        self._cache[session.key] = session
        workspace_key = session.metadata.get("workspace_key", "__workspace__")
        extra = {k: v for k, v in session.metadata.items() if k != "workspace_key"}
        try:
            await self._sqlite_manager.upsert_session_metadata(
                session_id=session.key,
                last_consolidated=session.last_consolidated,
                workspace_key=workspace_key,
                extra_metadata=extra,
            )
        except Exception:
            logger.exception("Failed to persist session metadata for {}", session.key)
        logger.debug(
            f"Session {session.key} saved (last_consolidated={session.last_consolidated})."
        )

    async def invalidate(self, key: str) -> None:
        """Remove a session from cache."""
        self._cache.pop(key, None)
        logger.info(f"Session {key} invalidated from cache.")

    async def add_message(
        self,
        session: Session,
        role: str,
        content: str,
        raw_data: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> int:
        """Persist a message to SQLite and append it to the in-memory session.

        raw_data should be the full message dict so agentic fields (tool_calls,
        tool_call_id, name) survive a reload.
        """
        presented_data_context = kwargs.pop("presented_data_context", None)
        message_id = await self._sqlite_manager.insert_message(
            session_id=session.key,
            role=role,
            content=content,
            presented_data_context=presented_data_context,
            raw_data=raw_data,
        )
        msg: Dict[str, Any] = raw_data.copy() if raw_data else {"role": role, "content": content}
        msg["id"] = message_id
        msg.setdefault("timestamp", datetime.now().isoformat())
        if presented_data_context:
            msg["presented_data_context"] = presented_data_context
        session.messages.append(msg)
        session.updated_at = datetime.now()
        return message_id

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """List all sessions by distinct session_id from the messages table."""
        await self._sqlite_manager._get_conn()
        session_keys = await self._sqlite_manager.list_session_keys()
        return [{"key": key, "path": str(self._sqlite_manager.db_path)} for key in session_keys]

    async def close(self) -> None:
        """Close is a no-op here — connection lifetime is owned by the shared SqliteManager."""
        logger.info(f"HybridSessionManager for workspace {self.workspace} closed (connection managed externally).")


_WORKSPACE_MEMORY_KEY = "__workspace__"
"""
Constant key used for the workspace-level memory snapshot (analogous to MEMORY.md).

The original file-based memory used a single MEMORY.md shared across ALL sessions
in a workspace. We preserve that behavior by keying the snapshot on this constant
rather than on the per-chat session UUID.  History entries (append_history) remain
session-scoped so their origin is traceable.
"""


class HybridMemoryStore:
    """
    Two-layer memory using SQLite (structured) + zvec (HNSW semantic search).

    Replaces the file-based MemoryStore when hybrid memory is enabled.
    Implements the same interface as MemoryStore (get_memory_context, consolidate,
    write_long_term, append_history) without inheriting from it to avoid the
    circular import: stores → agent.memory → agent.__init__ → loop → stores.

    The SqliteManager is shared with HybridSessionManager — do not close it here.
    """

    def __init__(
        self,
        workspace: Path,
        sqlite_manager: SqliteManager,
        zvec_manager: ZvecManager,
        provider: LLMProvider,
    ):
        self.workspace = workspace
        self._sqlite_manager = sqlite_manager
        self._zvec_manager = zvec_manager
        self.provider = provider
        logger.debug(f"HybridMemoryStore initialized for workspace {workspace}")

    def _resolve_workspace_key(self, workspace_key: str) -> str:
        """Return the effective memory snapshot key.

        If an account_id was threaded through (as workspace_<accountId>), use it.
        Otherwise fall back to the global workspace constant so all sessions that
        don't supply an account see the same shared memory.
        """
        return workspace_key if workspace_key else _WORKSPACE_MEMORY_KEY

    # Max chars of a user query to embed — prevents sending entire pasted documents.
    _MAX_QUERY_EMBED = 500
    # Max total chars of the assembled memory context injected into system prompt.
    _MAX_CONTEXT_CHARS = 6_000

    async def get_memory_context(
        self, session_id: str = "", query: str = "", workspace_key: str = ""
    ) -> str:
        """Return the memory context string to inject into the system prompt.

        workspace_key scopes memory to a logical tenant (e.g. "workspace_acct123").
        If not provided, falls back to the global _WORKSPACE_MEMORY_KEY so
        sessions without an account share the same workspace memory.

        If a query is provided and the embedding model is configured, a semantic
        search surfaces relevant past history entries to prepend.
        """
        effective_key = self._resolve_workspace_key(workspace_key)
        parts: List[str] = []

        if query and self._zvec_manager:
            # Truncate query so we never embed an entire pasted document.
            embed_query = query[: self._MAX_QUERY_EMBED]

            try:
                # Search history entries for relevant past conversations
                hist_results = await self._zvec_manager.semantic_search(
                    embed_query,
                    k=5,
                    filters={"type": "history_entry", "workspace_key": effective_key},
                )
                if hist_results:
                    lines = [f"- {r[2].get('text', '')}" for r in hist_results if r[2].get("text")]
                    if lines:
                        parts.append("## Relevant History\n" + "\n".join(lines))
            except Exception as e:
                logger.warning(f"Semantic search (history) failed: {e}")

            try:
                # Search discovery labels so users can recall past datasets by description
                # (e.g. "that fintech dataset from last week")
                disc_results = await self._zvec_manager.semantic_search(
                    embed_query,
                    k=3,
                    filters={"type": "discovery_result", "workspace_key": effective_key},
                )
                if disc_results:
                    lines = [
                        f"- {r[2].get('text', '')} (discovery id: {r[2].get('discovery_id', '?')})"
                        for r in disc_results
                        if r[2].get("text")
                    ]
                    if lines:
                        parts.append("## Past Discovery Datasets\n" + "\n".join(lines))
            except Exception as e:
                logger.warning(f"Semantic search (discovery) failed: {e}")

        snapshot = await self._sqlite_manager.get_memory_snapshot(effective_key)
        if snapshot:
            parts.append(f"## Long-term Memory\n{snapshot}")

        result = "\n\n".join(parts) if parts else ""
        # Cap total memory context to prevent system prompt overflow.
        if len(result) > self._MAX_CONTEXT_CHARS:
            result = result[: self._MAX_CONTEXT_CHARS] + "\n\n[Memory context truncated]"
        return result

    async def write_long_term(
        self,
        session_id: str,
        content: str,
        associated_message_id: Optional[int] = None,
        workspace_key: str = "",
    ) -> None:
        """Overwrite the memory snapshot for the effective workspace key.

        workspace_key = "workspace_<accountId>" when an account_id was supplied
        by the client, otherwise the global _WORKSPACE_MEMORY_KEY is used so all
        sessions share one memory — matching the original MEMORY.md behaviour.
        """
        effective_key = self._resolve_workspace_key(workspace_key)
        await self._sqlite_manager.upsert_memory_snapshot(effective_key, content)

        if self._zvec_manager:
            try:
                await self._zvec_manager.add_embedding(
                    content_id=f"snapshot:{effective_key}",
                    text=content,
                    metadata={
                        "type": "memory_snapshot",
                        "workspace_key": effective_key,
                        "text": content,
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to index memory snapshot in embeddings: {e}")

        logger.debug(f"Memory snapshot updated for workspace_key={effective_key!r}.")

    async def append_history(
        self,
        session_id: str,
        entry: str,
        associated_message_id: Optional[int] = None,
        workspace_key: str = "",
    ) -> None:
        """Append a history summary entry (analogous to HISTORY.md append)."""
        if not entry or not entry.strip():
            logger.debug("Skipping empty history entry for session {}", session_id)
            return
        effective_key = self._resolve_workspace_key(workspace_key)
        ltm_id = await self._sqlite_manager.insert_long_term_memory(
            session_id=session_id,
            text_content=entry,
            associated_entity_type="history_entry",
            associated_entity_id=associated_message_id,
        )
        if self._zvec_manager:
            try:
                await self._zvec_manager.add_embedding(
                    content_id=str(ltm_id),
                    text=entry,
                    metadata={
                        "session_id": session_id,
                        "workspace_key": effective_key,
                        "type": "history_entry",
                        "text": entry,
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to index history entry in embeddings: {e}")

        logger.debug(f"History entry (id: {ltm_id}) appended for session {session_id}.")

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into long-term memory via LLM tool call.

        Returns False when:
        - LLM provider.chat() raises (no API key, network error, rate limit, model error).
        - LLM response has no tool call (has_tool_calls is False).
        - save_memory arguments are missing or wrong type (e.g. not a dict after JSON parse).
        - append_history() or write_long_term() raises (SQLite/DB error).
        - Any other exception in the try block (e.g. get_messages_for_session, get_memory_snapshot).
        """
        # Re-sync session messages from DB to avoid stale in-memory state (atomic swap)
        current_messages_data = await self._sqlite_manager.get_messages_for_session(
            session.key, limit=-1
        )
        new_messages: List[dict] = []
        for msg_row in current_messages_data:
            if msg_row.get("raw_data"):
                msg = dict(msg_row["raw_data"])
            else:
                msg = {"role": msg_row["role"], "content": msg_row["content"]}
            msg.setdefault("timestamp", msg_row.get("timestamp", ""))
            new_messages.append(msg)
        session.messages = new_messages

        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info(
                "Memory consolidation: {} to consolidate, {} keep",
                len(old_messages), keep_count,
            )

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            lines.append(
                f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}: {m['content']}"
            )

        # workspace_key is stored in session.metadata by AgentLoop when an account_id
        # is provided by the client. Falls back to the global key if absent.
        workspace_key = session.metadata.get("workspace_key", _WORKSPACE_MEMORY_KEY)
        current_memory = await self._sqlite_manager.get_memory_snapshot(workspace_key) or ""

        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

        _SAVE_MEMORY_TOOL = [
            {
                "type": "function",
                "function": {
                    "name": "save_memory",
                    "description": "Save the memory consolidation result to persistent storage.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "history_entry": {
                                "type": "string",
                                "description": (
                                    "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                                    "Start with [YYYY-MM-DD HH:MM]. Include detail useful for search."
                                ),
                            },
                            "memory_update": {
                                "type": "string",
                                "description": (
                                    "Full updated long-term memory as markdown. Include all existing "
                                    "facts plus new ones. Return unchanged if nothing new."
                                ),
                            },
                        },
                        "required": ["history_entry", "memory_update"],
                    },
                },
            }
        ]

        try:
            response = await provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a memory consolidation agent. Call the save_memory tool.",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            # Some providers return arguments as a JSON string — normalise to dict.
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                logger.warning(
                    "Memory consolidation: unexpected arguments type {}", type(args).__name__
                )
                return False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                await self.append_history(session.key, entry, workspace_key=workspace_key)

            if update := args.get("memory_update"):
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    await self.write_long_term(session.key, update, workspace_key=workspace_key)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info(
                "Memory consolidation done: {} messages, last_consolidated={}",
                len(session.messages), session.last_consolidated,
            )
            return True
        except Exception:
            logger.exception("HybridMemoryStore consolidation failed")
            return False

    async def index_discovery_label(
        self,
        label: str,
        discovery_id: int,
        workspace_key: str = "",
    ) -> None:
        """Embed a discovery result label in zvec for cross-session semantic recall.

        The label (e.g. "hp-discovery: 142 rows [id, name, country, revenue]") is embedded
        so that future sessions can find datasets by description via semantic_search with
        type="discovery_result". The full payload is NOT embedded — only the compact label.

        Silently no-ops if zvec is not ready (missing embedding model or zvec not installed).
        """
        if not self._zvec_manager:
            return
        effective_key = self._resolve_workspace_key(workspace_key)
        try:
            await self._zvec_manager.add_embedding(
                content_id=f"disc_{discovery_id}",
                text=label,
                metadata={
                    "type": "discovery_result",
                    "workspace_key": effective_key,
                    "discovery_id": str(discovery_id),
                    "text": label,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to index discovery label in zvec: {e}")

    async def close(self) -> None:
        """Close zvec. The SqliteManager is owned by the caller (AgentLoop) — do not close it here.

        Exit-time errors we guard against: CancelledError (event loop shutting down),
        logger already torn down, or ZvecManager.close() raising during process exit.
        """
        try:
            if self._zvec_manager:
                await self._zvec_manager.close()
            logger.info(f"HybridMemoryStore for workspace {self.workspace} closed.")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error closing HybridMemoryStore")
