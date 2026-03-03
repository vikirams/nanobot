from __future__ import annotations

import aiosqlite


async def init_db(conn: aiosqlite.Connection) -> None:
    """
    Initializes the SQLite database schema on an already-open connection.

    Takes a live connection so callers never open a second concurrent connection
    to the same file (the previous implementation used aiosqlite.connect(path)
    internally, creating a redundant second connection on every startup).

    All CREATE TABLE / CREATE INDEX statements are idempotent (IF NOT EXISTS).
    Migrations for columns added after initial release use try/except ALTER TABLE.
    """
    await conn.execute("PRAGMA journal_mode = WAL;")
    await conn.execute("PRAGMA foreign_keys = ON;")

    # 1. Core Chat Messages
    # raw_data stores the full message dict (role, content, tool_calls, tool_call_id, name, …)
    # so that agentic multi-turn conversations can be faithfully reconstructed.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            presented_data_context TEXT,
            raw_data JSON
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages (session_id);")

    # 2. General-Purpose Session Entities (The "Data Lake" for all discovered data)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS session_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            external_id TEXT,
            data JSON NOT NULL,
            source_message_id INTEGER,
            last_interaction_message_id INTEGER,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(session_id, entity_type, external_id),
            FOREIGN KEY(source_message_id) REFERENCES messages(id),
            FOREIGN KEY(last_interaction_message_id) REFERENCES messages(id)
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_session_entities_session_id ON session_entities (session_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_session_entities_entity_type ON session_entities (entity_type);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_session_entities_external_id ON session_entities (external_id);")

    # 3. Message-Entity Link
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS message_entity_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            session_entity_id INTEGER NOT NULL,
            link_type TEXT NOT NULL,
            FOREIGN KEY(message_id) REFERENCES messages(id),
            FOREIGN KEY(session_entity_id) REFERENCES session_entities(id),
            UNIQUE(message_id, session_entity_id)
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_message_entity_links_message_id ON message_entity_links (message_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_message_entity_links_session_entity_id ON message_entity_links (session_entity_id);")

    # 4. User Selections/Bookmarks
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_selections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            session_entity_id INTEGER NOT NULL,
            selection_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            label TEXT,
            FOREIGN KEY(session_entity_id) REFERENCES session_entities(id),
            UNIQUE(session_id, session_entity_id, label)
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_selections_session_id ON user_selections (session_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_selections_timestamp ON user_selections (selection_timestamp);")

    # 5. Long-term History Entries (append-only log, analogous to HISTORY.md)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            text_content TEXT NOT NULL,
            associated_entity_type TEXT,
            associated_entity_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_long_term_memory_session_id ON long_term_memory (session_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_long_term_memory_associated_entity ON long_term_memory (associated_entity_type, associated_entity_id);")

    # 6. Memory Snapshot — one row per workspace/account key (MEMORY.md equivalent).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            text_content TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 7. Session Metadata — persists last_consolidated and workspace_key across restarts.
    # Without this table, a process restart resets last_consolidated to 0, causing
    # all messages to be re-consolidated on the next turn.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS session_metadata (
            session_id TEXT PRIMARY KEY,
            last_consolidated INTEGER NOT NULL DEFAULT 0,
            workspace_key TEXT NOT NULL DEFAULT '__workspace__',
            extra_metadata JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # 8. Discovery Results — one row per MCP discovery tool result per session.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS discovery_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            tool_name TEXT NOT NULL,
            query_or_label TEXT,
            payload TEXT NOT NULL,
            shape TEXT,
            row_count INTEGER DEFAULT 0
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_results_session_id ON discovery_results (session_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_results_created ON discovery_results (session_id, created_at DESC);")

    # 9. Cross-session canonical entities — global dedup key store across all sessions.
    # One row per unique real-world contact or company, keyed by email / linkedin_url / domain.
    # NULL values are allowed (SQLite treats NULL != NULL in UNIQUE checks, so multiple
    # rows with NULL email are permitted — dedup logic lives in the application layer).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type  TEXT NOT NULL,           -- 'contact' | 'company'
            email        TEXT,
            linkedin_url TEXT,
            domain       TEXT,
            data         JSON NOT NULL DEFAULT '{}',
            first_seen   DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_seen    DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type    ON entities (entity_type);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_email   ON entities (email);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_linkedin ON entities (linkedin_url);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_domain  ON entities (domain);")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_dedup_email ON entities (entity_type, email) WHERE email IS NOT NULL;"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_dedup_linkedin ON entities (entity_type, linkedin_url) WHERE linkedin_url IS NOT NULL;"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_dedup_domain ON entities (entity_type, domain) WHERE domain IS NOT NULL;"
    )

    # 10. Entity-session mapping: records which sessions surfaced each entity (many-to-many).
    # Enables cross-session questions: "Which sessions found this company?"
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_sessions (
            entity_id  INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            seen_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (entity_id, session_id),
            FOREIGN KEY (entity_id) REFERENCES entities (id) ON DELETE CASCADE
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_sessions_session ON entity_sessions (session_id);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_sessions_entity  ON entity_sessions (entity_id);")

    # 11. MCP result cache — cross-session 48-hour cache keyed by hash of (tool_name + params).
    # Avoids re-calling MCP for identical queries within the TTL window.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_cache (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            cache_key  TEXT NOT NULL UNIQUE,
            tool_name  TEXT NOT NULL,
            params_json TEXT,
            result_json TEXT NOT NULL,
            row_count  INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME NOT NULL
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_cache_key ON mcp_cache (cache_key);")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_cache_expires ON mcp_cache (expires_at);")

    # Migrations for columns added after initial release — safe to ignore if already exist.
    for _col_ddl in (
        "ALTER TABLE discovery_results ADD COLUMN shape TEXT;",
        "ALTER TABLE discovery_results ADD COLUMN row_count INTEGER DEFAULT 0;",
        "ALTER TABLE session_entities ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP;",
    ):
        try:
            await conn.execute(_col_ddl)
        except Exception:
            pass  # Column already exists

    await conn.commit()
