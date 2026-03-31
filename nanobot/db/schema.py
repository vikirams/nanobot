"""PostgreSQL schema initialisation — idempotent DDL for all Nanobot tables."""
from __future__ import annotations

import asyncpg
from loguru import logger


async def init_schema(pool: asyncpg.Pool) -> None:
    """Create all tables and indexes if they do not yet exist.

    Safe to call on every startup — all statements use IF NOT EXISTS.
    The pgvector extension must already be enabled before this runs
    (connection.get_pool() handles that).
    """
    async with pool.acquire() as conn:
        # ── Accounts ──────────────────────────────────────────────────────────
        # One row per account. Primary key is the only row-level index needed
        # (always looked up by account_id). GIN index on company_dna enables
        # efficient key-existence and containment queries on the JSONB column.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id   text PRIMARY KEY,
                company_dna  jsonb NOT NULL DEFAULT '{}',
                created_at   timestamptz DEFAULT now(),
                updated_at   timestamptz DEFAULT now()
            );
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_accounts_dna "
            "ON accounts USING GIN (company_dna jsonb_path_ops);"
        )

        # ── Sessions ──────────────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id        text PRIMARY KEY,
                account_id        text NOT NULL,
                user_id           text NOT NULL,
                channel           text NOT NULL DEFAULT 'web',
                status            text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','archived','suspended')),
                last_consolidated int NOT NULL DEFAULT 0,
                metadata          jsonb,
                created_at        timestamptz DEFAULT now(),
                updated_at        timestamptz DEFAULT now()
            );
        """)
        # Drop legacy columns that were never written by app code.
        for col in ("system_prompt_overrides", "active_tools", "current_plan_id", "parsed_filters"):
            await conn.execute(
                f"ALTER TABLE agent_sessions DROP COLUMN IF EXISTS {col};"
            )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_sessions_account_user "
            "ON agent_sessions (account_id, user_id);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_sessions_account_status "
            "ON agent_sessions (account_id, status);"
        )

        # ── Messages ──────────────────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS session_messages (
                id              bigserial PRIMARY KEY,
                message_id      text NOT NULL UNIQUE DEFAULT gen_random_uuid()::text,
                session_id      text NOT NULL
                    REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
                account_id      text NOT NULL,
                user_id         text NOT NULL,
                role            text NOT NULL
                    CHECK (role IN ('user','assistant','system','tool')),
                content         jsonb NOT NULL,
                tool_name       text,
                resultset_ref   text,
                token_count     int,
                metadata        jsonb,
                created_at      timestamptz DEFAULT now()
            );
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_messages_session "
            "ON session_messages (session_id, created_at);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_messages_account_user "
            "ON session_messages (account_id, user_id, created_at DESC);"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_messages_resultset "
            "ON session_messages (resultset_ref) "
            "WHERE resultset_ref IS NOT NULL;"
        )

        # ── Account-level long-term memory ────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS account_memory (
                account_id  text PRIMARY KEY,
                snapshot    text NOT NULL,
                updated_at  timestamptz DEFAULT now()
            );
        """)

        # ── Append-only history log ───────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_history (
                id          bigserial PRIMARY KEY,
                account_id  text NOT NULL,
                user_id     text NOT NULL,
                session_id  text NOT NULL,
                entry       text NOT NULL,
                created_at  timestamptz DEFAULT now()
            );
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_history_account_user "
            "ON memory_history (account_id, user_id, created_at DESC);"
        )

        # ── Semantic embeddings ───────────────────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                id            bigserial PRIMARY KEY,
                content_id    text NOT NULL,
                account_id    text NOT NULL,
                user_id       text NOT NULL,
                content_type  text NOT NULL
                    CHECK (content_type IN ('history_entry','resultset_label')),
                text_content  text NOT NULL,
                embedding     vector(1536),
                metadata      jsonb,
                created_at    timestamptz DEFAULT now(),
                UNIQUE (account_id, content_id)
            );
        """)
        # HNSW index for semantic search (requires pgvector extension).
        # ef_construction=128 doubles recall vs 64 at minimal build-time cost.
        # NOTE: IF NOT EXISTS skips recreation on existing DBs — drop and recreate
        # the index manually if upgrading: DROP INDEX idx_memory_embeddings_hnsw;
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hnsw "
            "ON memory_embeddings USING hnsw (embedding vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 128);"
        )
        # Time-weighted semantic search support (recency + relevance queries).
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_created_at "
            "ON memory_embeddings (account_id, created_at DESC);"
        )

    logger.info("PostgreSQL schema initialised")
