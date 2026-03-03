# Hybrid Memory

Two-layer memory for nanobot: **SQLite** for structured persistence and **ZvecManager** (alibaba/zvec HNSW) for semantic search.

## What SQLite does

A single workspace database (`~/.nanobot/storage/<workspace_hash>/workspace.db`) stores:

| Table | Purpose |
|-------|---------|
| **messages** | Chat messages per session (role, content, raw_data for tool_calls). Replaces file-based session history. |
| **memory_snapshots** | One row per workspace key: the current "MEMORY.md" content. Overwritten on each consolidation. |
| **long_term_memory** | Append-only history entries (HISTORY.md equivalent). Each consolidation adds a summary line. |
| **session_entities** / **message_entity_links** / **user_selections** | Optional "data lake" for discovered entities and links. |

**SqliteManager** is the single async connection to this DB. It is shared by:

- **HybridSessionManager** — load/save sessions and messages.
- **HybridMemoryStore** — read/write memory snapshots and history, and coordinate with ZvecManager for embeddings.

## What Zvec (ZvecManager) does

**ZvecManager** provides **semantic search** over memory so the agent can retrieve "relevant past context" for the current query instead of only the latest snapshot.

1. **Storage**
   Embeddings are stored in a zvec HNSW collection at `~/.nanobot/storage/<workspace_hash>/zvec/`. This is a separate path from SQLite, managed entirely by zvec.

2. **Indexing**
   When the agent:
   - updates long-term memory (`write_long_term`) or
   - appends a history entry (`append_history`),
   the store asks ZvecManager to **embed** that text (via the LLM provider's `embed()` API) and **upsert** it into the zvec collection with metadata (`type`, `workspace_key`).

3. **Search**
   When building context for a turn, `get_memory_context(session_id, query=..., workspace_key=...)` calls **semantic_search(query, k=5, filters=...)**. ZvecManager:
   - embeds the query,
   - calls `collection.query(vectors=VectorQuery(...), filter="type='...' and workspace_key='...'", topk=k)`,
   - returns the top-k entries sorted by cosine score.
   Those are rendered as a "Relevant History" section in the system prompt; the long-term memory snapshot is always included as "Long-term Memory".

4. **Fallback when zvec is unavailable**
   If **zvec** is not installed (Python >3.12 dev environments), `_is_ready()` is false. Then:
   - No embeddings are written,
   - `semantic_search` is not used,
   - `get_memory_context` only returns the **memory snapshot** (no "Relevant History").
   So hybrid memory still works; only the semantic layer is disabled.

## Dependencies

- **zvec** (`pip install zvec`) — required for semantic search. Wheels are provided for Python 3.10–3.12, Linux x86_64/ARM64, macOS ARM64. The `pyproject.toml` installs it conditionally: `python_version < '3.13'`.
- No numpy dependency.

## Flow summary

```
User message
    → get_memory_context(query=user_message, workspace_key=…)
        → [if zvec ready] semantic_search(query) → "Relevant History" (top-k history entries)
        → get_memory_snapshot(workspace_key) → "Long-term Memory"
    → System prompt = identity + bootstrap + Memory (Relevant History + Long-term Memory) + skills
    → LLM turn

After turn / consolidation:
    → write_long_term(...) or append_history(...)
        → SQLite: update memory_snapshots or insert long_term_memory
        → [if zvec ready] add_embedding(...) → zvec collection (HNSW upsert)
```
