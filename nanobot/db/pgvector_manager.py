"""Semantic embedding manager backed by PostgreSQL + pgvector."""
from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any, Optional

import asyncpg
from loguru import logger

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

# Maximum characters of a query to embed.  1 200 chars covers most multi-sentence
# prompts without sending entire pasted documents; previous 500 was too aggressive
# and caused the "core intent" at the end of long queries to be silently dropped.
_MAX_QUERY_EMBED = 1200


class PGVectorManager:
    """
    Stores and retrieves semantic embeddings using PostgreSQL + pgvector.

    Vectors are passed as strings '[1.0,2.0,...]' with ::vector cast in SQL.
    Cosine distance is computed with the <=> operator; score = 1 - distance.

    Gracefully no-ops when no embedding model is configured on the provider.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        provider: "LLMProvider",
        embedding_dim: int = 1536,
    ) -> None:
        self._pool = pool
        self._provider = provider
        self._embedding_dim = embedding_dim

    async def _get_embedding(self, text: str) -> list[float] | None:
        """Compute an embedding vector for *text* using the configured provider.

        Returns None if no embedding model is configured or if the call fails.
        embed() must return a plain list[float] — see LiteLLMProvider.embed().
        """
        try:
            model = self._provider.get_embedding_model()
            if not model:
                logger.debug("No embedding model configured — skipping embedding")
                return None
            embedding = await self._provider.embed(input=[text[: _MAX_QUERY_EMBED]], model=model)
            if not isinstance(embedding, list) or not embedding:
                logger.warning(
                    "Embedding returned unexpected type {} — expected list[float]. "
                    "Check that embed() unpacks EmbeddingResponse.data[0].embedding.",
                    type(embedding).__name__,
                )
                return None
            if len(embedding) != self._embedding_dim:
                logger.warning(
                    "Embedding dim mismatch: expected {}, got {}. "
                    "Update database.embedding_dim in config to match your model.",
                    self._embedding_dim,
                    len(embedding),
                )
                return None
            return embedding
        except AttributeError:
            logger.warning(
                "Embedding skipped — provider does not implement embed(). "
                "Set agents.defaults.embedding_model in config."
            )
            return None
        except Exception as e:
            logger.warning("Failed to compute embedding: {}", e)
            return None

    @staticmethod
    def _vec_to_str(vec: list[float]) -> str:
        """Serialise a float list to pgvector literal '[1.0,2.0,...]'."""
        return "[" + ",".join(str(v) for v in vec) + "]"

    async def add_embedding(
        self,
        content_id: str,
        account_id: str,
        user_id: str,
        content_type: str,
        text: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Compute an embedding for *text* and upsert it into memory_embeddings.

        No-ops silently if the embedding model is not available.
        content_type must be 'history_entry' or 'resultset_label'.
        """
        vec = await self._get_embedding(text)
        if vec is None:
            return

        vec_str = self._vec_to_str(vec)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO memory_embeddings
                    (content_id, account_id, user_id, content_type, text_content, embedding, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb)
                ON CONFLICT (account_id, content_id) DO UPDATE SET
                    content_type = EXCLUDED.content_type,
                    text_content = EXCLUDED.text_content,
                    embedding    = EXCLUDED.embedding,
                    metadata     = EXCLUDED.metadata
                """,
                content_id, account_id, user_id, content_type, text, vec_str, meta_json,
            )

    async def semantic_search(
        self,
        query: str,
        account_id: str,
        user_id: str,
        k: int = 5,
        content_types: Optional[list[str]] = None,
        min_score: float = 0.0,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """Search for the k most semantically similar embeddings.

        Filters by account_id (required) and optionally by user_id and content_type.
        Returns list of (content_id, score, metadata_dict) where score = 1 - cosine_distance.
        Pass min_score (e.g. 0.7) to suppress low-relevance noise from the prompt.
        Returns [] if no embedding model is configured or search fails.
        """
        vec = await self._get_embedding(query)
        if vec is None:
            return []

        vec_str = self._vec_to_str(vec)

        try:
            async with self._pool.acquire() as conn:
                if content_types:
                    # Build placeholder list: $4, $5, ...
                    type_placeholders = ", ".join(
                        f"${i}" for i in range(5, 5 + len(content_types))
                    )
                    sql = f"""
                        SELECT content_id,
                               1 - (embedding <=> $1::vector) AS score,
                               metadata
                        FROM memory_embeddings
                        WHERE account_id = $2
                          AND user_id    = $3
                          AND content_type IN ({type_placeholders})
                        ORDER BY embedding <=> $1::vector
                        LIMIT $4
                    """
                    rows = await conn.fetch(
                        sql, vec_str, account_id, user_id, k, *content_types
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT content_id,
                               1 - (embedding <=> $1::vector) AS score,
                               metadata
                        FROM memory_embeddings
                        WHERE account_id = $2
                          AND user_id    = $3
                        ORDER BY embedding <=> $1::vector
                        LIMIT $4
                        """,
                        vec_str, account_id, user_id, k,
                    )
        except Exception as e:
            logger.warning("Semantic search failed: {}", e)
            return []

        results = []
        for row in rows:
            score = float(row["score"])
            if not math.isfinite(score):
                continue
            if score < min_score:
                continue
            meta = row["metadata"]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            elif meta is None:
                meta = {}
            results.append((row["content_id"], score, meta))
        return results
