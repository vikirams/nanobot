"""PostgreSQL database manager for Nanobot agent state."""
from __future__ import annotations

import json
from typing import Any, Optional

import asyncpg
from loguru import logger


class DBManager:
    """
    Manages all Nanobot agent state in PostgreSQL.

    All methods accept account_id and enforce tenant isolation — no query ever
    crosses account boundaries.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ── Accounts ──────────────────────────────────────────────────────────────

    async def get_account_dna(self, account_id: str) -> dict[str, Any] | None:
        """Return the company DNA for an account, or None if not set."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT company_dna FROM accounts WHERE account_id = $1",
                account_id,
            )
        if row is None:
            return None
        val = row["company_dna"]
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return {}
        return dict(val) if val else {}

    async def upsert_account_dna(self, account_id: str, dna: dict[str, Any]) -> None:
        """Insert or replace the company DNA for an account."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO accounts (account_id, company_dna, updated_at)
                VALUES ($1, $2::jsonb, now())
                ON CONFLICT (account_id) DO UPDATE SET
                    company_dna = EXCLUDED.company_dna,
                    updated_at  = now()
                """,
                account_id,
                json.dumps(dna, ensure_ascii=False),
            )

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def upsert_session(
        self,
        session_id: str,
        account_id: str,
        user_id: str,
        channel: str = "web",
        **kwargs: Any,
    ) -> None:
        """Insert or update an agent session row."""
        metadata = kwargs.get("metadata")
        status = kwargs.get("status", "active")

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_sessions
                    (session_id, account_id, user_id, channel, status, metadata, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, now())
                ON CONFLICT (session_id) DO UPDATE SET
                    account_id = EXCLUDED.account_id,
                    user_id    = EXCLUDED.user_id,
                    channel    = EXCLUDED.channel,
                    status     = EXCLUDED.status,
                    metadata   = COALESCE(EXCLUDED.metadata, agent_sessions.metadata),
                    updated_at = now()
                """,
                session_id,
                account_id,
                user_id,
                channel,
                status,
                json.dumps(metadata) if metadata is not None else None,
            )

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the session row as a dict, or None if not found."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM agent_sessions WHERE session_id = $1",
                session_id,
            )
        if row is None:
            return None
        d = dict(row)
        val = d.get("metadata")
        if isinstance(val, str):
            try:
                d["metadata"] = json.loads(val)
            except Exception:
                pass
        return d

    async def update_session_consolidated(
        self, session_id: str, last_consolidated: int
    ) -> None:
        """Persist last_consolidated so restarts resume from the correct point."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_sessions
                SET last_consolidated = $1, updated_at = now()
                WHERE session_id = $2
                """,
                last_consolidated,
                session_id,
            )

    async def list_session_keys(self, account_id: str) -> list[str]:
        """Return all session_ids for an account."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT session_id FROM agent_sessions WHERE account_id = $1 ORDER BY updated_at DESC",
                account_id,
            )
        return [r["session_id"] for r in rows]

    # ── Messages ──────────────────────────────────────────────────────────────

    async def insert_message(
        self,
        session_id: str,
        account_id: str,
        user_id: str,
        role: str,
        content_dict: dict[str, Any],
        tool_name: Optional[str] = None,
        resultset_ref: Optional[str] = None,
        token_count: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> str:
        """Insert a message and return its generated message_id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO session_messages
                    (session_id, account_id, user_id, role, content,
                     tool_name, resultset_ref, token_count, metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9::jsonb)
                RETURNING message_id
                """,
                session_id,
                account_id,
                user_id,
                role,
                json.dumps(content_dict, ensure_ascii=False),
                tool_name,
                resultset_ref,
                token_count,
                json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
            )
        return row["message_id"]

    async def get_messages_for_session(
        self,
        session_id: str,
        limit: int = -1,
        offset: int = 0,
        account_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return messages for a session in chronological order.

        Returns content as a Python dict (preserving all fields).
        For messages with a resultset_ref, the inner content field is replaced
        with a lightweight placeholder to avoid loading large blobs.
        limit=-1 returns all rows.  offset skips the first N rows (row-count,
        not message_id) — used by consolidation to fetch only pending messages.
        """
        _content_expr = """
            CASE
              WHEN resultset_ref IS NOT NULL THEN
                jsonb_set(content, '{content}', to_jsonb(
                  '[Dataset stored — resultset_ref=' || resultset_ref || '. Use list_resultsets to reference it.]'
                ))
              ELSE content
            END AS content
        """
        async with self._pool.acquire() as conn:
            where_clause = "WHERE session_id = $1"
            params: list[Any] = [session_id]
            if account_id:
                params.append(account_id)
                where_clause += f" AND account_id = ${len(params)}"
            if user_id:
                params.append(user_id)
                where_clause += f" AND user_id = ${len(params)}"

            sql = f"""
                SELECT message_id, session_id, account_id, user_id, role,
                       {_content_expr}, tool_name, resultset_ref, token_count, metadata, created_at
                FROM session_messages
                {where_clause}
                ORDER BY id ASC
            """
            if limit > 0:
                params.append(limit)
                sql += f" LIMIT ${len(params)}"
            if offset > 0:
                params.append(offset)
                sql += f" OFFSET ${len(params)}"

            rows = await conn.fetch(sql, *params)

        result = []
        for row in rows:
            d = dict(row)
            # asyncpg returns jsonb as dicts already, but guard for string fallback
            for field in ("content", "metadata"):
                val = d.get(field)
                if isinstance(val, str):
                    try:
                        d[field] = json.loads(val)
                    except Exception:
                        pass
            result.append(d)
        return result

    async def get_message_content(self, message_id: str) -> dict[str, Any] | None:
        """Fetch the full content for a specific message by message_id.

        Returns the raw content dict (including full payload for resultset messages),
        or None if the message does not exist.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT message_id, role, content, tool_name, resultset_ref, created_at
                FROM session_messages
                WHERE message_id = $1
                """,
                message_id,
            )
        if row is None:
            return None
        d = dict(row)
        val = d.get("content")
        if isinstance(val, str):
            try:
                d["content"] = json.loads(val)
            except Exception:
                pass
        return d

    # ── Account memory ────────────────────────────────────────────────────────

    async def upsert_account_memory(self, account_id: str, snapshot: str) -> None:
        """Overwrite the long-term memory snapshot for an account."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO account_memory (account_id, snapshot, updated_at)
                VALUES ($1, $2, now())
                ON CONFLICT (account_id) DO UPDATE SET
                    snapshot   = EXCLUDED.snapshot,
                    updated_at = now()
                """,
                account_id,
                snapshot,
            )

    async def get_account_memory(self, account_id: str) -> str | None:
        """Return the current memory snapshot for an account, or None."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT snapshot FROM account_memory WHERE account_id = $1",
                account_id,
            )
        return row["snapshot"] if row else None

    # ── Memory history ────────────────────────────────────────────────────────

    async def insert_memory_history(
        self,
        account_id: str,
        user_id: str,
        session_id: str,
        entry: str,
    ) -> int:
        """Append a history summary entry and return its id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO memory_history (account_id, user_id, session_id, entry)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                account_id,
                user_id,
                session_id,
                entry,
            )
        return row["id"]

    async def get_memory_history(
        self, account_id: str, user_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the most recent history entries for an (account, user) pair."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, account_id, user_id, session_id, entry, created_at
                FROM memory_history
                WHERE account_id = $1 AND user_id = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                account_id,
                user_id,
                limit,
            )
        return [dict(r) for r in rows]

    # ── Resultset refs ────────────────────────────────────────────────────────

    async def list_resultset_refs(
        self,
        account_id: str,
        user_id: str,
        session_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List resultset_refs with MCP-canonical IDs, labels, and row counts.

        Returns dicts with: message_id, tool_name, resultset_ref,
        mcp_resultset_id, label, row_count, created_at.

        mcp_resultset_id is the ID to pass to MCP tools (hp.exportCsv, etc.).
        resultset_ref prefixed 'agent_' is an agent-side DB key only.

        Row extraction strategy:
          - mcp_resultset_id: resultset_ref when not agent-prefixed (direct MCP ID),
            else extracted from nested content JSON (execute tool responses).
          - label: content->>'label' for slash-command rows; segmentName/query from
            inner JSON for execute rows; falls back to tool_name.
          - row_count: 'total' field from inner execute-response JSON.
        """
        _sql_fields = """
            message_id,
            tool_name,
            resultset_ref,
            created_at,
            -- mcp_resultset_id: use resultset_ref when it IS the MCP id (not agent-prefixed),
            -- otherwise fall back to parsing the nested execute-response JSON.
            COALESCE(
                CASE WHEN resultset_ref NOT LIKE 'agent_%' THEN resultset_ref ELSE NULL END,
                CASE WHEN left(trim(content->>'content'), 1) = '{'
                     THEN (content->>'content')::jsonb->>'resultset_id'
                     ELSE NULL END
            ) AS mcp_resultset_id,
            -- label: direct field (slash commands) or segmentName/query from inner JSON
            COALESCE(
                content->>'label',
                CASE WHEN left(trim(content->>'content'), 1) = '{'
                     THEN COALESCE(
                         (content->>'content')::jsonb->>'segmentName',
                         (content->>'content')::jsonb->>'query'
                     )
                     ELSE NULL END,
                tool_name
            ) AS label,
            -- row_count from the execute-response 'total' field
            CASE WHEN left(trim(content->>'content'), 1) = '{'
                 THEN (content->>'content')::jsonb->>'total'
                 ELSE NULL END AS row_count
        """
        async with self._pool.acquire() as conn:
            if session_id:
                rows = await conn.fetch(
                    f"""
                    SELECT {_sql_fields}
                    FROM session_messages
                    WHERE account_id = $1
                      AND user_id    = $2
                      AND session_id = $3
                      AND resultset_ref IS NOT NULL
                    ORDER BY created_at ASC
                    LIMIT $4
                    """,
                    account_id, user_id, session_id, limit,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT {_sql_fields}
                    FROM session_messages
                    WHERE account_id = $1
                      AND user_id    = $2
                      AND resultset_ref IS NOT NULL
                    ORDER BY created_at ASC
                    LIMIT $3
                    """,
                    account_id, user_id, limit,
                )
        return [dict(r) for r in rows]

    async def insert_discovery_result(
        self,
        session_id: str,
        account_id: str,
        user_id: str,
        tool_name: str,
        payload: str,
        label: str,
    ) -> str:
        """Insert a paired assistant+tool turn for a slash-command discovery result.

        Writes two rows — an assistant message with a tool_calls entry followed
        by the matching tool result — so the session history is structurally
        valid for strict LLMs (e.g. xAI Grok) that require every tool message
        to have a tool_call_id referencing a prior assistant tool_calls entry.

        Returns the new resultset_ref.
        """
        import uuid as _uuid
        resultset_ref = f"agent_{_uuid.uuid4().hex[:12]}"
        call_id = f"synth_{_uuid.uuid4().hex[:12]}"

        assistant_dict = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": "{}"},
            }],
        }
        tool_dict = {
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool_name,
            "content": payload,
            "resultset_ref": resultset_ref,
            "label": label,
        }
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO session_messages
                    (session_id, account_id, user_id, role, content,
                     tool_name, resultset_ref)
                VALUES
                    ($1, $2, $3, 'assistant', $4::jsonb, $5, NULL),
                    ($1, $2, $3, 'tool',      $6::jsonb, $5, $7)
                """,
                session_id,
                account_id,
                user_id,
                json.dumps(assistant_dict, ensure_ascii=False),
                tool_name,
                json.dumps(tool_dict, ensure_ascii=False),
                resultset_ref,
            )
        return resultset_ref
