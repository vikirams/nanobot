from __future__ import annotations

import asyncio
import hashlib
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from loguru import logger


def get_workspace_db_path(workspace: Path) -> Path:
    """
    Generates a workspace-specific SQLite database path using an MD5 hash of the workspace's absolute path.
    This ensures isolation for multi-tenancy.
    """
    workspace_abs_path = workspace.absolute()
    ws_hash = hashlib.md5(str(workspace_abs_path).encode("utf-8")).hexdigest()
    db_dir = Path.home() / ".nanobot" / "storage" / ws_hash
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "workspace.db"


def _sanitize_account_id(account_id: str) -> str:
    """Sanitize account_id for safe use as a directory name."""
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", account_id)
    return safe[:128] or "_default"


def get_account_db_path(account_id: str) -> Path:
    """Per-account SQLite path: ~/.nanobot/accounts/<account_id>/workspace.db"""
    safe = _sanitize_account_id(account_id)
    account_dir = Path.home() / ".nanobot" / "accounts" / safe
    account_dir.mkdir(parents=True, exist_ok=True)
    return account_dir / "workspace.db"


class SqliteManager:
    """
    Manages asynchronous SQLite interactions for a single workspace database.
    Focuses on performance and memory efficiency by managing connections and using aiosqlite.
    """

    def __init__(self, workspace: Path, db_path: Optional[Path] = None):
        # db_path overrides the auto-derived path, enabling per-account databases.
        self.db_path = db_path if db_path is not None else get_workspace_db_path(workspace)
        self._conn: Optional[aiosqlite.Connection] = None
        self._init_lock = asyncio.Lock()

    async def _get_conn(self) -> aiosqlite.Connection:
        """Get or create an aiosqlite connection, initialising the schema on first open."""
        if self._conn is None:
            async with self._init_lock:
                if self._conn is None:  # Double-checked locking
                    logger.debug(f"Opening new SQLite connection for {self.db_path}")
                    self._conn = await aiosqlite.connect(self.db_path)
                    await self._conn.execute("PRAGMA journal_mode = WAL;")
                    await self._conn.execute("PRAGMA foreign_keys = ON;")
                    # Pass the already-open connection so init_db never opens a
                    # second concurrent connection to the same file.
                    from nanobot.hybrid_memory.db_schema import init_db
                    await init_db(self._conn)
        return self._conn

    async def close(self):
        """Close the database connection."""
        async with self._init_lock:
            if self._conn:
                logger.debug(f"Closing SQLite connection for {self.db_path}")
                await self._conn.close()
                self._conn = None

    async def insert_message(
        self,
        session_id: str,
        role: str,
        content: str,
        presented_data_context: Optional[str] = None,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Inserts a new message into the messages table.

        raw_data should be the full message dict (including tool_calls, tool_call_id, name, etc.)
        so that agentic conversations can be faithfully reconstructed on reload.
        """
        conn = await self._get_conn()
        raw_data_json = json.dumps(raw_data, ensure_ascii=False) if raw_data is not None else None
        cursor = await conn.execute(
            """
            INSERT INTO messages (session_id, role, content, presented_data_context, raw_data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, content, presented_data_context, raw_data_json),
        )
        await conn.commit()
        return cursor.lastrowid

    async def get_messages_for_session(self, session_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        """Retrieves messages for a given session.

        Returns the full raw_data dict when available so that tool_calls, tool_call_id,
        and other agentic fields are preserved across reloads.
        """
        conn = await self._get_conn()
        query = """
            SELECT id, session_id, role, content, timestamp, presented_data_context, raw_data
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
        """
        params: List[Any] = [session_id]
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        cursor = await conn.execute(query, tuple(params))
        rows = await cursor.fetchall()
        messages = []
        for row in rows:
            raw_data = json.loads(row[6]) if row[6] else None
            messages.append(
                {
                    "id": row[0],
                    "session_id": row[1],
                    "role": row[2],
                    "content": row[3],
                    "timestamp": row[4],
                    "presented_data_context": row[5],
                    "raw_data": raw_data,
                }
            )
        return messages

    async def upsert_session_entity(
        self,
        session_id: str,
        entity_type: str,
        external_id: str,
        data: Dict[str, Any],
        source_message_id: int,
        last_interaction_message_id: int,
    ) -> int:
        """
        Inserts or updates a session entity.
        If an entity with the same session_id, entity_type, and external_id exists, it's updated.
        Otherwise, a new entity is inserted.
        """
        conn = await self._get_conn()
        data_json = json.dumps(data, ensure_ascii=False)
        try:
            cursor = await conn.execute(
                """
                INSERT INTO session_entities (session_id, entity_type, external_id, data, source_message_id, last_interaction_message_id)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, entity_type, external_id) DO UPDATE SET
                    data = EXCLUDED.data,
                    last_interaction_message_id = EXCLUDED.last_interaction_message_id;
                """,
                (
                    session_id,
                    entity_type,
                    external_id,
                    data_json,
                    source_message_id,
                    last_interaction_message_id,
                ),
            )
            await conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error upserting session entity: {e}")
            await conn.rollback()
            raise

    async def get_session_entities(
        self,
        session_id: str,
        entity_ids: Optional[List[int]] = None,
        entity_type: Optional[str] = None,
        limit: int = -1,
    ) -> List[Dict[str, Any]]:
        """Retrieves session entities based on session_id, optional entity_ids, and entity_type."""
        conn = await self._get_conn()
        query = "SELECT id, session_id, entity_type, external_id, data, source_message_id, last_interaction_message_id FROM session_entities WHERE session_id = ?"
        params: List[Any] = [session_id]

        if entity_type:
            query += " AND entity_type = ?"
            params.append(entity_type)
        if entity_ids:
            placeholders = ",".join(["?"] * len(entity_ids))
            query += f" AND id IN ({placeholders})"
            params.extend(entity_ids)

        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        cursor = await conn.execute(query, tuple(params))
        rows = await cursor.fetchall()
        entities = []
        for row in rows:
            entities.append(
                {
                    "id": row[0],
                    "session_id": row[1],
                    "entity_type": row[2],
                    "external_id": row[3],
                    "data": json.loads(row[4]),
                    "source_message_id": row[5],
                    "last_interaction_message_id": row[6],
                }
            )
        return entities

    async def insert_message_entity_link(self, message_id: int, session_entity_id: int, link_type: str):
        """Links a message to a session entity."""
        conn = await self._get_conn()
        try:
            await conn.execute(
                """
                INSERT INTO message_entity_links (message_id, session_entity_id, link_type)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id, session_entity_id) DO NOTHING;
                """,
                (message_id, session_entity_id, link_type),
            )
            await conn.commit()
        except Exception as e:
            logger.error(f"Error inserting message entity link: {e}")
            await conn.rollback()
            raise

    async def get_linked_entities_for_message(self, message_id: int) -> List[Dict[str, Any]]:
        """Retrieves all entities linked to a specific message."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            """
            SELECT se.id, se.session_id, se.entity_type, se.external_id, se.data, se.source_message_id, se.last_interaction_message_id
            FROM session_entities se
            JOIN message_entity_links mel ON se.id = mel.session_entity_id
            WHERE mel.message_id = ?
            """,
            (message_id,),
        )
        rows = await cursor.fetchall()
        entities = []
        for row in rows:
            entities.append(
                {
                    "id": row[0],
                    "session_id": row[1],
                    "entity_type": row[2],
                    "external_id": row[3],
                    "data": json.loads(row[4]),
                    "source_message_id": row[5],
                    "last_interaction_message_id": row[6],
                }
            )
        return entities

    async def insert_long_term_memory(
        self,
        session_id: str,
        text_content: str,
        associated_entity_type: Optional[str] = None,
        associated_entity_id: Optional[int] = None,
    ) -> int:
        """Inserts a new long-term memory entry."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            """
            INSERT INTO long_term_memory (session_id, text_content, associated_entity_type, associated_entity_id)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, text_content, associated_entity_type, associated_entity_id),
        )
        await conn.commit()
        return cursor.lastrowid

    async def get_long_term_memory(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves long-term memory entries for a session."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            """
            SELECT id, session_id, text_content, associated_entity_type, associated_entity_id, created_at
            FROM long_term_memory
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        memory_entries = []
        for row in rows:
            memory_entries.append(
                {
                    "id": row[0],
                    "session_id": row[1],
                    "text_content": row[2],
                    "associated_entity_type": row[3],
                    "associated_entity_id": row[4],
                    "created_at": row[5],
                }
            )
        return memory_entries

    async def list_session_keys(self) -> List[str]:
        """Lists all unique session keys present in the database."""
        conn = await self._get_conn()
        cursor = await conn.execute("SELECT DISTINCT session_id FROM messages;")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def upsert_memory_snapshot(self, session_id: str, text_content: str) -> int:
        """Insert or replace the memory snapshot for a session.

        This is the MEMORY.md equivalent — one row per session, overwritten on each
        consolidation so memory doesn't grow unboundedly.
        """
        conn = await self._get_conn()
        cursor = await conn.execute(
            """
            INSERT INTO memory_snapshots (session_id, text_content, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                text_content = EXCLUDED.text_content,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (session_id, text_content),
        )
        await conn.commit()
        return cursor.lastrowid

    async def get_memory_snapshot(self, session_id: str) -> Optional[str]:
        """Retrieve the current memory snapshot for a session, or None if none exists."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT text_content FROM memory_snapshots WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ── Discovery results (MCP discovery tool result storage) ─────────────────

    async def insert_discovery_result(
        self,
        session_id: str,
        tool_name: str,
        payload: str,
        query_or_label: Optional[str] = None,
        shape: Optional[str] = None,
        row_count: int = 0,
    ) -> int:
        """Insert a discovery tool result. Returns the row id (discovery index for this session).

        shape: "array" | "wrapped" | "object" — the JSON shape of the payload.
        row_count: pre-computed row count so list queries don't re-parse the full payload.
        """
        conn = await self._get_conn()
        cursor = await conn.execute(
            """
            INSERT INTO discovery_results (session_id, tool_name, query_or_label, payload, shape, row_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, tool_name, query_or_label or None, payload, shape, row_count),
        )
        await conn.commit()
        return cursor.lastrowid

    async def list_discovery_results(self, session_id: str) -> List[Dict[str, Any]]:
        """List discovery results for a session, ordered by creation (1-based index = row order).

        Uses pre-computed row_count and shape columns to avoid re-parsing large payloads.
        Falls back to payload inspection for rows written before these columns were added.
        """
        conn = await self._get_conn()
        cursor = await conn.execute(
            """
            SELECT id, created_at, tool_name, query_or_label, payload, shape, row_count
            FROM discovery_results
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        out = []
        for i, row in enumerate(rows, start=1):
            stored_shape = row[5]
            stored_row_count = row[6]

            # For rows written before shape/row_count columns were added, fall back to parsing.
            if stored_shape is None:
                payload_str = row[4] or "[]"
                try:
                    data = json.loads(payload_str)
                    if isinstance(data, dict) and isinstance(data.get("data"), list):
                        stored_row_count = len(data["data"])
                        stored_shape = "wrapped"
                    elif isinstance(data, list):
                        stored_row_count = len(data)
                        stored_shape = "array"
                    else:
                        stored_row_count = 0
                        stored_shape = "object"
                except Exception:
                    stored_row_count = 0

            out.append({
                "id": row[0],
                "index": i,
                "label": row[3] or f"Discovery #{i}",
                "rows": stored_row_count or 0,
                "shape": stored_shape,
                "created_at": row[1],
                "tool_name": row[2],
            })
        return out

    async def get_discovery_result(
        self, session_id: str, which: str | int
    ) -> Optional[Tuple[int, str, str]]:
        """
        Get a single discovery result by 'last', 1-based index, or id.
        Returns (discovery_id, payload_json_string, query_or_label) or None.
        """
        conn = await self._get_conn()
        if which == "last" or (isinstance(which, str) and which.strip().lower() == "last"):
            cursor = await conn.execute(
                """
                SELECT id, payload, query_or_label FROM discovery_results
                WHERE session_id = ? ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            )
        else:
            idx = which
            if isinstance(which, str) and which.strip().isdigit():
                idx = int(which.strip())
            if not isinstance(idx, int) or idx < 1:
                return None
            cursor = await conn.execute(
                """
                SELECT id, payload, query_or_label FROM discovery_results
                WHERE session_id = ?
                ORDER BY id ASC
                LIMIT 1 OFFSET ?
                """,
                (session_id, idx - 1),
            )
        row = await cursor.fetchone()
        if not row:
            return None
        return (row[0], row[1] or "[]", row[2] or "")

    # ── Session metadata (last_consolidated + workspace_key persistence) ──────

    async def upsert_session_metadata(
        self,
        session_id: str,
        last_consolidated: int,
        workspace_key: str = "__workspace__",
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist session metadata so it survives process restarts.

        Without this, last_consolidated resets to 0 on every restart and all
        messages are re-consolidated unnecessarily on the next turn.
        """
        conn = await self._get_conn()
        meta_json = json.dumps(extra_metadata or {}, ensure_ascii=False)
        await conn.execute(
            """
            INSERT INTO session_metadata (session_id, last_consolidated, workspace_key, extra_metadata, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                last_consolidated = EXCLUDED.last_consolidated,
                workspace_key = EXCLUDED.workspace_key,
                extra_metadata = EXCLUDED.extra_metadata,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (session_id, last_consolidated, workspace_key, meta_json),
        )
        await conn.commit()

    # ── MCP result cache ──────────────────────────────────────────────────────

    async def get_mcp_cache(self, cache_key: str) -> Optional[str]:
        """Return cached MCP result JSON if a valid (non-expired) entry exists, else None."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT result_json FROM mcp_cache WHERE cache_key = ? AND expires_at > CURRENT_TIMESTAMP",
            (cache_key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_mcp_cache(
        self,
        cache_key: str,
        tool_name: str,
        params_json: str,
        result_json: str,
        ttl_hours: int = 48,
    ) -> None:
        """Upsert an MCP result into the cache with a TTL. Updates expiry on conflict."""
        row_count = 0
        try:
            data = json.loads(result_json)
            if isinstance(data, list):
                row_count = len(data)
            elif isinstance(data, dict) and isinstance(data.get("data"), list):
                row_count = len(data["data"])
        except Exception:
            pass
        conn = await self._get_conn()
        await conn.execute(
            """
            INSERT INTO mcp_cache (cache_key, tool_name, params_json, result_json, row_count, expires_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', '+' || ? || ' hours'))
            ON CONFLICT(cache_key) DO UPDATE SET
                result_json = EXCLUDED.result_json,
                row_count   = EXCLUDED.row_count,
                created_at  = CURRENT_TIMESTAMP,
                expires_at  = EXCLUDED.expires_at;
            """,
            (cache_key, tool_name, params_json, result_json, row_count, str(ttl_hours)),
        )
        await conn.commit()

    # ── Cross-session canonical entities ─────────────────────────────────────

    async def upsert_entity(
        self,
        entity_type: str,
        data: Dict[str, Any],
        session_id: str,
        email: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> int:
        """Upsert a canonical entity and link it to a session.

        Deduplication priority: email → linkedin_url → domain (for companies).
        When a match is found, enriches the existing record with any new non-null
        fields from *data* rather than overwriting, so we always keep the richest
        version of each entity.  Returns the entity id.
        """
        conn = await self._get_conn()
        email = (email or "").strip() or None
        linkedin_url = (linkedin_url or "").strip() or None
        domain = (domain or "").strip() or None
        data_json = json.dumps(data, ensure_ascii=False)

        entity_id: Optional[int] = None

        # Priority 1: match on email
        if email:
            cursor = await conn.execute(
                "SELECT id, data FROM entities WHERE entity_type = ? AND email = ?",
                (entity_type, email),
            )
            row = await cursor.fetchone()
            if row:
                entity_id = row[0]

        # Priority 2: match on linkedin_url
        if entity_id is None and linkedin_url:
            cursor = await conn.execute(
                "SELECT id, data FROM entities WHERE entity_type = ? AND linkedin_url = ?",
                (entity_type, linkedin_url),
            )
            row = await cursor.fetchone()
            if row:
                entity_id = row[0]

        # Priority 3: match on domain (mainly for companies)
        if entity_id is None and domain:
            cursor = await conn.execute(
                "SELECT id, data FROM entities WHERE entity_type = ? AND domain = ?",
                (entity_type, domain),
            )
            row = await cursor.fetchone()
            if row:
                entity_id = row[0]

        if entity_id is not None:
            # Merge: load existing data, update only fields that are currently null/empty
            existing_cursor = await conn.execute(
                "SELECT data FROM entities WHERE id = ?", (entity_id,)
            )
            existing_row = await existing_cursor.fetchone()
            if existing_row:
                try:
                    existing_data = json.loads(existing_row[0]) if existing_row[0] else {}
                except Exception:
                    existing_data = {}
                for k, v in data.items():
                    if v is not None and v != "" and (existing_data.get(k) is None or existing_data.get(k) == ""):
                        existing_data[k] = v
                data_json = json.dumps(existing_data, ensure_ascii=False)

            await conn.execute(
                """
                UPDATE entities SET
                    data = ?,
                    last_seen = CURRENT_TIMESTAMP,
                    email = COALESCE(?, email),
                    linkedin_url = COALESCE(?, linkedin_url),
                    domain = COALESCE(?, domain)
                WHERE id = ?
                """,
                (data_json, email, linkedin_url, domain, entity_id),
            )
        else:
            cursor = await conn.execute(
                """
                INSERT INTO entities (entity_type, email, linkedin_url, domain, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entity_type, email, linkedin_url, domain, data_json),
            )
            entity_id = cursor.lastrowid

        # Link entity to this session (idempotent)
        await conn.execute(
            "INSERT OR IGNORE INTO entity_sessions (entity_id, session_id) VALUES (?, ?)",
            (entity_id, session_id),
        )
        await conn.commit()
        return entity_id

    async def get_entities_for_session(
        self,
        session_id: str,
        entity_type: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return all entities seen in a session, ordered by last_seen DESC."""
        conn = await self._get_conn()
        q = """
            SELECT e.id, e.entity_type, e.email, e.linkedin_url, e.domain,
                   e.data, e.first_seen, e.last_seen
            FROM entities e
            JOIN entity_sessions es ON es.entity_id = e.id
            WHERE es.session_id = ?
        """
        params: List[Any] = [session_id]
        if entity_type:
            q += " AND e.entity_type = ?"
            params.append(entity_type)
        q += " ORDER BY e.last_seen DESC LIMIT ?"
        params.append(limit)
        cursor = await conn.execute(q, tuple(params))
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "entity_type": r[1], "email": r[2],
                "linkedin_url": r[3], "domain": r[4],
                "data": json.loads(r[5]) if r[5] else {},
                "first_seen": r[6], "last_seen": r[7],
            }
            for r in rows
        ]

    async def search_entities(
        self,
        entity_type: Optional[str] = None,
        email: Optional[str] = None,
        domain: Optional[str] = None,
        keyword: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Search entities across all sessions by email, domain, or data keyword."""
        conn = await self._get_conn()
        q = """
            SELECT id, entity_type, email, linkedin_url, domain, data, first_seen, last_seen
            FROM entities WHERE 1=1
        """
        params: List[Any] = []
        if entity_type:
            q += " AND entity_type = ?"
            params.append(entity_type)
        if email:
            q += " AND email = ?"
            params.append(email)
        if domain:
            q += " AND domain = ?"
            params.append(domain)
        if keyword:
            q += " AND data LIKE ?"
            params.append(f"%{keyword}%")
        q += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        cursor = await conn.execute(q, tuple(params))
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "entity_type": r[1], "email": r[2],
                "linkedin_url": r[3], "domain": r[4],
                "data": json.loads(r[5]) if r[5] else {},
                "first_seen": r[6], "last_seen": r[7],
            }
            for r in rows
        ]

    async def get_session_metadata(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve persisted session metadata, or None if this session has no saved metadata."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT last_consolidated, workspace_key, extra_metadata FROM session_metadata WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        extra = json.loads(row[2]) if row[2] else {}
        return {
            "last_consolidated": row[0],
            "workspace_key": row[1],
            "extra_metadata": extra,
        }
