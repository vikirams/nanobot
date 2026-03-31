from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, List, TYPE_CHECKING

from loguru import logger

from nanobot.session.manager import Session
from nanobot.providers.base import LLMProvider

if TYPE_CHECKING:
    from nanobot.db.manager import DBManager
    from nanobot.db.pgvector_manager import PGVectorManager


class HybridSessionManager:
    """
    Manages conversation sessions backed by PostgreSQL.

    Session cache uses LRU eviction to bound memory (default max 500 entries).
    """

    _CACHE_MAX_SIZE = 500

    def __init__(
        self,
        workspace: Path,
        db_manager: "DBManager",
    ) -> None:
        self.workspace = workspace
        self._db_manager = db_manager
        self._cache: OrderedDict[str, Session] = OrderedDict()

    async def get_or_create(
        self,
        key: str,
        account_id: str = "",
        user_id: str = "",
        channel: str = "web",
    ) -> Session:
        """Get an existing session from the DB or create a new one."""
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        session = await self._load_session(key, account_id, user_id, channel)

        if session is None:
            logger.info("Creating new session for key: {}", key)
            session = Session(key=key)
            if account_id:
                try:
                    await self._db_manager.upsert_session(
                        session_id=key,
                        account_id=account_id,
                        user_id=user_id,
                        channel=channel,
                    )
                except Exception:
                    logger.exception("Failed to upsert session row for {}", key)

        while len(self._cache) >= self._CACHE_MAX_SIZE:
            self._cache.popitem(last=False)
        self._cache[key] = session
        return session

    async def _load_session(
        self,
        key: str,
        account_id: str,
        user_id: str,
        channel: str,
    ) -> Session | None:
        """Load (or create) a session row in Postgres and reconstruct messages."""
        try:
            await self._db_manager.upsert_session(
                session_id=key,
                account_id=account_id,
                user_id=user_id,
                channel=channel,
            )
            session_row = await self._db_manager.get_session(key)
        except Exception:
            logger.exception("Failed to load session {} from Postgres", key)
            return None

        try:
            messages_data = await self._db_manager.get_messages_for_session(key, limit=-1)
        except Exception:
            logger.exception("Failed to load messages for session {}", key)
            messages_data = []

        if not messages_data:
            last_consolidated = session_row.get("last_consolidated", 0) if session_row else 0
            return Session(key=key, last_consolidated=last_consolidated)

        messages = []
        for msg_row in messages_data:
            content_raw = msg_row.get("content", {})
            if isinstance(content_raw, dict):
                msg = dict(content_raw)
            else:
                try:
                    msg = json.loads(content_raw)
                except Exception:
                    msg = {"role": msg_row.get("role", "user"), "content": str(content_raw)}
            msg.setdefault("timestamp", str(msg_row.get("created_at", datetime.now().isoformat())))
            messages.append(msg)

        if messages:
            try:
                created_at = datetime.fromisoformat(str(messages[0]["timestamp"]).replace("Z", "+00:00"))
            except Exception:
                created_at = datetime.now()
            try:
                updated_at = datetime.fromisoformat(str(messages[-1]["timestamp"]).replace("Z", "+00:00"))
            except Exception:
                updated_at = datetime.now()
        else:
            created_at = updated_at = datetime.now()

        last_consolidated = session_row.get("last_consolidated", 0) if session_row else 0
        logger.info(
            "Loaded {} messages for session {} (last_consolidated={})",
            len(messages), key, last_consolidated,
        )
        return Session(
            key=key,
            messages=messages,
            created_at=created_at,
            updated_at=updated_at,
            last_consolidated=last_consolidated,
        )

    async def save(self, session: Session) -> None:
        """Persist session metadata and update in-memory cache."""
        if session.key in self._cache:
            self._cache.move_to_end(session.key)
        else:
            while len(self._cache) >= self._CACHE_MAX_SIZE:
                self._cache.popitem(last=False)
        self._cache[session.key] = session

        try:
            await self._db_manager.update_session_consolidated(
                session.key, session.last_consolidated
            )
        except Exception:
            logger.exception("Failed to persist session metadata for {}", session.key)

        logger.debug("Session {} saved (last_consolidated={}).", session.key, session.last_consolidated)

    async def invalidate(self, key: str) -> None:
        """Remove a session from cache."""
        self._cache.pop(key, None)
        logger.info("Session {} invalidated from cache.", key)

    async def add_message(
        self,
        session: Session,
        role: str,
        content: str,
        raw_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> int:
        """Append message to in-memory session.

        In the Postgres path messages are saved directly via DBManager.insert_message()
        in _save_turn — this method only updates the in-memory session object.
        """
        msg: dict[str, Any] = raw_data.copy() if raw_data else {"role": role, "content": content}
        msg.setdefault("timestamp", datetime.now().isoformat())
        session.messages.append(msg)
        session.updated_at = datetime.now()
        return -1

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List sessions — returns empty list (no global account listing without account_id)."""
        return []

    async def close(self) -> None:
        """Close is a no-op — connection lifetime is owned by the shared pool."""
        logger.info(
            "HybridSessionManager for workspace {} closed (connection managed externally).",
            self.workspace,
        )


_WORKSPACE_MEMORY_KEY = "__workspace__"
"""
Constant key used for the workspace-level memory snapshot (analogous to MEMORY.md).
"""


class HybridMemoryStore:
    """
    Two-layer memory backed by PostgreSQL + pgvector.

    Implements get_memory_context, consolidate, write_long_term, append_history.
    """

    _MAX_QUERY_EMBED = 1200   # match pgvector_manager; 500 silently dropped intent at end of long queries
    _MAX_CONTEXT_CHARS = 6_000
    # Only inject memories whose cosine similarity exceeds this threshold.
    # Anything below 0.70 is usually noise and inflates the agent's context window.
    _MIN_SCORE = 0.70

    def __init__(
        self,
        workspace: Path,
        db_manager: "DBManager",
        vec_manager: "PGVectorManager | None",
        provider: LLMProvider,
    ) -> None:
        self.workspace = workspace
        self.provider = provider
        self._db_manager: "DBManager" = db_manager
        self._pgvec_manager: "PGVectorManager | None" = vec_manager

        logger.debug("HybridMemoryStore initialised (postgres=True)")

    # ── get_memory_context ────────────────────────────────────────────────────

    async def get_memory_context(
        self,
        session_id: str = "",
        query: str = "",
        account_id: str = "",
        user_id: str = "",
        **_kwargs: Any,
    ) -> str:
        """Return the memory context string to inject into the system prompt."""
        parts: List[str] = []

        if query and self._pgvec_manager and account_id and user_id:
            embed_query = query[: self._MAX_QUERY_EMBED]

            try:
                results = await self._pgvec_manager.semantic_search(
                    embed_query,
                    account_id=account_id,
                    user_id=user_id,
                    k=8,
                    content_types=["history_entry", "resultset_label"],
                    min_score=self._MIN_SCORE,
                )
                hist_lines = []
                data_lines = []
                for content_id, _score, meta in results:
                    ctype = meta.get("content_type") or meta.get("type", "")
                    text = meta.get("text_content") or meta.get("text", "")
                    if not text:
                        continue
                    if ctype == "resultset_label" or "resultset_ref" in meta:
                        ref = meta.get("resultset_ref", "?")
                        rows_count = meta.get("row_count", "?")
                        fields_str = ", ".join(meta.get("fields", []))
                        fetched = meta.get("fetched_at", "")
                        line = (
                            f'- "{text}" → resultset_ref={ref} | {rows_count} rows'
                            + (f" | fields: {fields_str}" if fields_str else "")
                            + (f" | fetched {fetched}" if fetched else "")
                        )
                        data_lines.append(line)
                    else:
                        hist_lines.append(f"- {text}")

                if data_lines:
                    parts.append(
                        "## Past Related Data You Have Already Collected\n"
                        + "\n".join(data_lines)
                        + "\n  → Before fetching fresh data, use this resultset_ref if the "
                        "user's request is a subset (e.g. city filter)."
                    )
                if hist_lines:
                    parts.append("## Relevant History\n" + "\n".join(hist_lines))
            except Exception as e:
                logger.warning("Semantic search skipped: {}", e)

        if account_id:
            try:
                snapshot = await self._db_manager.get_account_memory(account_id)
                if snapshot:
                    parts.append(f"## Long-term Memory\n{snapshot}")
            except Exception as e:
                logger.warning("Failed to load account memory: {}", e)

        if not parts:
            return ""

        # Truncate per-section before joining so no section completely dominates.
        # Each section gets a fair share; anything beyond its allocation is clipped.
        per_section = max(1200, self._MAX_CONTEXT_CHARS // max(len(parts), 1))
        trimmed: List[str] = []
        for part in parts:
            if len(part) > per_section:
                part = part[:per_section] + "\n  [... truncated]"
            trimmed.append(part)

        result = "\n\n".join(trimmed)
        if len(result) > self._MAX_CONTEXT_CHARS:
            result = result[: self._MAX_CONTEXT_CHARS] + "\n\n[Memory context truncated]"
        return result

    # ── write_long_term ───────────────────────────────────────────────────────

    async def write_long_term(
        self,
        session_id: str,
        content: str,
        account_id: str = "",
        **_kwargs: Any,
    ) -> None:
        """Overwrite the memory snapshot."""
        if not account_id:
            logger.warning("write_long_term called without account_id")
            return
        await self._db_manager.upsert_account_memory(account_id, content)
        logger.debug("Memory snapshot updated.")

    # ── append_history ────────────────────────────────────────────────────────

    async def append_history(
        self,
        session_id: str,
        entry: str,
        account_id: str = "",
        user_id: str = "",
        **_kwargs: Any,
    ) -> None:
        """Append a history summary entry."""
        if not entry or not entry.strip():
            logger.debug("Skipping empty history entry for session {}", session_id)
            return

        if not account_id:
            logger.warning("append_history called without account_id")
            return
        try:
            hist_id = await self._db_manager.insert_memory_history(
                account_id=account_id,
                user_id=user_id,
                session_id=session_id,
                entry=entry,
            )
            if self._pgvec_manager:
                try:
                    await self._pgvec_manager.add_embedding(
                        content_id=f"hist_{hist_id}",
                        account_id=account_id,
                        user_id=user_id,
                        content_type="history_entry",
                        text=entry,
                        metadata={
                            "content_type": "history_entry",
                            "session_id": session_id,
                            "text_content": entry,
                            "text": entry,
                        },
                    )
                except Exception as e:
                    logger.warning("Failed to index history entry in pgvector: {}", e)
        except Exception:
            logger.exception("Failed to append history for session {}", session_id)

    # ── index_resultset_label ─────────────────────────────────────────────────

    async def index_resultset_label(
        self,
        resultset_ref: str,
        label: str,
        fields: list[str],
        row_count: int,
        account_id: str,
        user_id: str,
        session_id: str,
    ) -> None:
        """Embed a resultset label for future semantic recall."""
        if not self._pgvec_manager:
            return
        try:
            fetched_at = datetime.now().strftime("%Y-%m-%d")
            await self._pgvec_manager.add_embedding(
                content_id=f"rs_{resultset_ref}",
                account_id=account_id,
                user_id=user_id,
                content_type="resultset_label",
                text=label,
                metadata={
                    "content_type": "resultset_label",
                    "resultset_ref": resultset_ref,
                    "row_count": row_count,
                    "fields": fields,
                    "session_id": session_id,
                    "fetched_at": fetched_at,
                    "text_content": label,
                    "text": label,
                },
            )
        except Exception as e:
            logger.warning("Failed to index resultset label in pgvector: {}", e)

    # ── consolidate ───────────────────────────────────────────────────────────

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
        account_id: str = "",
        user_id: str = "",
    ) -> bool:
        """Consolidate old messages into long-term memory via LLM tool call.

        Only fetches the *pending* (not-yet-consolidated) portion of the history
        from the DB to avoid loading the entire message log into memory for long
        sessions.
        """
        # Fetch only messages since last_consolidated to bound memory usage.
        fetch_offset = 0 if archive_all else session.last_consolidated
        try:
            messages_data = await self._db_manager.get_messages_for_session(
                session.key, limit=-1, offset=fetch_offset
            )
        except Exception:
            logger.exception("Failed to reload messages for consolidation (session {})", session.key)
            return False

        pending_messages: list[dict] = []
        for msg_row in messages_data:
            content_raw = msg_row.get("content", {})
            if isinstance(content_raw, dict):
                msg = dict(content_raw)
            else:
                try:
                    msg = json.loads(content_raw)
                except Exception:
                    msg = {"role": msg_row.get("role", "user"), "content": str(content_raw)}
            ts = msg_row.get("created_at", "")
            msg.setdefault("timestamp", str(ts) if ts else "")
            pending_messages.append(msg)

        if archive_all:
            old_messages = pending_messages
            keep_count = 0
        else:
            keep_count = memory_window // 2
            if len(pending_messages) <= keep_count:
                return True
            old_messages = pending_messages[:-keep_count]
            if not old_messages:
                return True

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            lines.append(
                f"[{str(m.get('timestamp', '?'))[:16]}] {m['role'].upper()}: {m['content']}"
            )

        current_memory = ""
        if account_id:
            try:
                current_memory = await self._db_manager.get_account_memory(account_id) or ""
            except Exception:
                pass

        return await self._run_consolidation_llm(
            session=session,
            provider=provider,
            model=model,
            lines=lines,
            current_memory=current_memory,
            archive_all=archive_all,
            keep_count=keep_count,
            pending_count=len(pending_messages),
            fetch_offset=fetch_offset,
            account_id=account_id,
            user_id=user_id,
        )

    async def _run_consolidation_llm(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        lines: list[str],
        current_memory: str,
        archive_all: bool,
        keep_count: int,
        pending_count: int,
        fetch_offset: int,
        account_id: str = "",
        user_id: str = "",
    ) -> bool:
        """LLM call to produce memory consolidation.

        Retries once on tool-call failure to guard against transient LLM non-compliance.
        """
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.
You MUST call the save_memory tool — do not respond with plain text.

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

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a memory consolidation agent. "
                    "You MUST respond by calling the save_memory tool. "
                    "Never output plain text — only tool calls."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        # Attempt up to 2 times — retry once if LLM skips the tool call.
        for attempt in range(2):
            try:
                response = await provider.chat(
                    messages=messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                )

                if not response.has_tool_calls:
                    if attempt == 0:
                        logger.warning(
                            "Memory consolidation: LLM did not call save_memory (attempt 1), retrying"
                        )
                        # Give the model a nudge on the second attempt
                        messages = messages + [
                            {"role": "assistant", "content": response.content or ""},
                            {
                                "role": "user",
                                "content": "You must call the save_memory tool now. Do not reply with text.",
                            },
                        ]
                        continue
                    logger.warning("Memory consolidation: LLM skipped save_memory after retry, giving up")
                    return False

                args = response.tool_calls[0].arguments
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
                    await self.append_history(
                        session.key, entry,
                        account_id=account_id,
                        user_id=user_id,
                    )

                if update := args.get("memory_update"):
                    if not isinstance(update, str):
                        update = json.dumps(update, ensure_ascii=False)
                    if update != current_memory:
                        await self.write_long_term(
                            session.key, update,
                            account_id=account_id,
                        )

                # Advance the consolidated pointer by the number of messages we summarised.
                # fetch_offset is where pending messages began; pending_count - keep_count
                # is how many were summarised.
                if archive_all:
                    session.last_consolidated = 0
                else:
                    session.last_consolidated = fetch_offset + (pending_count - keep_count)

                logger.info(
                    "Memory consolidation done: pending={}, summarised={}, last_consolidated={}",
                    pending_count,
                    pending_count - keep_count,
                    session.last_consolidated,
                )
                return True

            except Exception:
                logger.exception("HybridMemoryStore consolidation failed (attempt {})", attempt + 1)
                if attempt == 0:
                    continue
                return False

        return False

    async def close(self) -> None:
        """Close is a no-op — pool lifetime is managed by AgentLoop."""
        logger.info("HybridMemoryStore for workspace {} closed.", self.workspace)
