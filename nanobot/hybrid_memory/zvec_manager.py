"""Semantic embedding manager backed by alibaba/zvec.

Uses the zvec in-process vector database (HNSW + cosine) for semantic search.
The collection is persisted at ~/.nanobot/storage/<workspace_hash>/zvec/ alongside
the SQLite database.

Falls back gracefully when zvec is not installed — this happens on Python >3.12
(zvec wheels are published for Python 3.10-3.12 only). The rest of the hybrid
memory system (SQLite messages, long-term memory snapshot) continues to work
normally; only the "Relevant History" semantic injection is disabled.

All zvec operations are synchronous C++ calls and are dispatched via
asyncio.get_running_loop().run_in_executor() to avoid blocking the event loop.
Each executor call is wrapped in asyncio.wait_for() to prevent hangs on disk I/O
stalls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from nanobot.providers.base import LLMProvider

# ---------------------------------------------------------------------------
# Embedding dimension lookup for common models
# ---------------------------------------------------------------------------
_MODEL_DIMS: Dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
_DEFAULT_DIM = 1536

# Executor timeout for all blocking zvec operations (seconds).
_EXEC_TIMEOUT = 30.0

# Guard so zvec.init() is called at most once per process.
_zvec_initialised = False
_zvec_init_lock = threading.Lock()


def _get_workspace_zvec_path(workspace: Path) -> Path:
    """Return the directory where the zvec collection lives for this workspace."""
    ws_hash = hashlib.md5(str(workspace.absolute()).encode("utf-8")).hexdigest()
    zvec_dir = Path.home() / ".nanobot" / "storage" / ws_hash / "zvec"
    # Only mkdir the parent — zvec.create_and_open() must create zvec_dir itself.
    zvec_dir.parent.mkdir(parents=True, exist_ok=True)
    return zvec_dir


def get_account_zvec_path(account_id: str) -> Path:
    """Per-account zvec collection path: ~/.nanobot/accounts/<account_id>/zvec/"""
    import re
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", account_id)[:128] or "_default"
    zvec_dir = Path.home() / ".nanobot" / "accounts" / safe / "zvec"
    # Only mkdir the parent — zvec.create_and_open() must create zvec_dir itself.
    zvec_dir.parent.mkdir(parents=True, exist_ok=True)
    return zvec_dir


def _ensure_zvec_init() -> None:
    """Call zvec.init() exactly once per process (suppresses noisy C++ logs)."""
    global _zvec_initialised
    if _zvec_initialised:
        return
    with _zvec_init_lock:
        if _zvec_initialised:
            return
        import zvec
        from zvec import LogLevel, LogType

        zvec.init(log_type=LogType.CONSOLE, log_level=LogLevel.ERROR)
        _zvec_initialised = True


class ZvecManager:
    """
    Manages semantic embeddings for workspace memory using zvec.

    Storage  : zvec HNSW collection at ~/.nanobot/storage/<hash>/zvec/
    Metric   : cosine similarity (MetricType.COSINE)
    Embeddings: generated via LLMProvider.embed() (litellm aembedding)
    Threading: all zvec calls run in a thread-pool executor with a 30s timeout
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        zvec_path: Optional[Path] = None,
    ):
        self._workspace = workspace
        self._provider = provider
        self._embedding_model = provider.get_embedding_model()
        self._zvec_available: Optional[bool] = None
        self._collection: Any = None
        self._init_lock = threading.Lock()
        # zvec_path overrides the auto-derived path, enabling per-account collections.
        self._zvec_path_override = zvec_path
        self._dim = self._infer_dim()
        logger.debug(
            "ZvecManager initialized. "
            f"Embedding model: {self._embedding_model or 'disabled'}, "
            f"dim={self._dim}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _infer_dim(self) -> int:
        """Return the embedding dimension for the configured model."""
        model = (self._embedding_model or "").lower()
        for key, dim in _MODEL_DIMS.items():
            if key in model:
                return dim
        return _DEFAULT_DIM

    def _is_ready(self) -> bool:
        """Return True when the embedding model is configured and zvec is installed."""
        if not self._embedding_model:
            return False
        if self._zvec_available is None:
            try:
                import zvec  # noqa: F401

                self._zvec_available = True
            except ImportError:
                logger.warning(
                    "zvec not installed — semantic search disabled. "
                    "Requires Python 3.10-3.12: pip install zvec"
                )
                self._zvec_available = False
        return bool(self._zvec_available)

    def _zvec_path(self) -> Path:
        """Return the effective zvec collection path."""
        return (
            self._zvec_path_override
            if self._zvec_path_override is not None
            else _get_workspace_zvec_path(self._workspace)
        )

    def _get_or_create_collection_sync(self) -> Any:
        """Open or create the zvec collection.  Must be called inside a thread.

        If open() fails for any reason (collection missing, empty dir, corruption):
          1. Remove the directory with shutil.rmtree so non-empty dirs are also cleaned.
          2. Recreate a fresh collection.

        This is safe because embeddings are regeneratable from the SQLite content.
        """
        if self._collection is not None:
            return self._collection

        with self._init_lock:
            if self._collection is not None:
                return self._collection

            _ensure_zvec_init()

            import zvec
            from zvec import (
                CollectionSchema,
                DataType,
                FieldSchema,
                HnswIndexParam,
                InvertIndexParam,
                MetricType,
                VectorQuery,
                VectorSchema,
            )

            zvec_path = self._zvec_path()
            # Ensure the parent exists but do NOT pre-create zvec_path itself.
            zvec_path.parent.mkdir(parents=True, exist_ok=True)

            def _make_schema() -> Any:
                return CollectionSchema(
                    name="memory_embeddings",
                    fields=[
                        FieldSchema(
                            "type",
                            DataType.STRING,
                            nullable=True,
                            index_param=InvertIndexParam(),
                        ),
                        FieldSchema(
                            "workspace_key",
                            DataType.STRING,
                            nullable=True,
                            index_param=InvertIndexParam(),
                        ),
                        FieldSchema("metadata_json", DataType.STRING, nullable=True),
                    ],
                    vectors=[
                        VectorSchema(
                            "embedding",
                            DataType.VECTOR_FP32,
                            dimension=self._dim,
                            index_param=HnswIndexParam(metric_type=MetricType.COSINE),
                        ),
                    ],
                )

            try:
                coll = zvec.open(path=str(zvec_path))
                # Probe: write a canary doc then immediately query for it.
                # zvec.open() can silently succeed even when the HNSW index is
                # corrupted — C++ errors are logged to stderr but never raised as
                # Python exceptions.  A failed write→read round-trip exposes this.
                _probe_vec = [0.0] * (self._dim - 1) + [1.0]
                coll.upsert(zvec.Doc(
                    id="__probe__",
                    fields={"type": "_probe_", "workspace_key": "", "metadata_json": "{}"},
                    vectors={"embedding": _probe_vec},
                ))
                _probe_results = coll.query(
                    vectors=VectorQuery("embedding", vector=_probe_vec),
                    topk=1,
                    filter="type='_probe_'",
                )
                if not _probe_results:
                    raise RuntimeError(
                        "zvec probe failed: upsert succeeded but query returned empty "
                        "— HNSW index is silently corrupted at C++ level"
                    )
                self._collection = coll
                logger.debug(f"Opened zvec collection at {zvec_path} (probe OK)")
            except Exception as open_err:
                # Release the corrupted handle BEFORE rmtree so RocksDB's
                # in-process lock on idmap.0/LOCK is freed.  Without this,
                # create_and_open() fails with "lock hold by current process".
                import gc

                try:
                    del coll
                except NameError:
                    pass
                gc.collect()

                if zvec_path.exists():
                    try:
                        shutil.rmtree(str(zvec_path))
                        logger.warning(
                            "Removed zvec directory at {} after open failure — rebuilding: {}",
                            zvec_path, open_err,
                        )
                    except Exception as rm_err:
                        logger.error(
                            "Could not remove zvec directory {}: {}", zvec_path, rm_err
                        )
                self._collection = zvec.create_and_open(
                    path=str(zvec_path), schema=_make_schema()
                )
                logger.debug(
                    f"Created fresh zvec collection at {zvec_path} (dim={self._dim})"
                )

        return self._collection

    def _reset_collection_sync(self) -> None:
        """Reset the in-memory collection handle and delete the on-disk zvec directory.

        Called when a corruption is detected so the next operation recreates a clean index.
        Must be called inside a thread (run_in_executor).
        """
        with self._init_lock:
            self._collection = None
            zvec_path = self._zvec_path()
            if zvec_path.exists():
                try:
                    shutil.rmtree(str(zvec_path))
                    logger.warning(f"Deleted corrupted zvec collection at {zvec_path} — will rebuild")
                except Exception as del_err:
                    logger.error(f"Could not delete zvec directory {zvec_path}: {del_err}")

    def _is_corruption_error(self, exc: Exception) -> bool:
        """Return True if the exception looks like a zvec index corruption or unrecoverable error."""
        msg = str(exc).lower()
        return any(kw in msg for kw in (
            "corrupt", "invalid", "segment", "panic", "hnswlib",
            "io error", "bad file", "failed to open", "no such file or directory",
            "permission denied",
        ))

    @staticmethod
    def _build_filter_string(filters: Optional[Dict[str, Any]]) -> Optional[str]:
        """Build a zvec filter expression, escaping single quotes in values."""
        if not filters:
            return None
        parts = []
        for field, value in filters.items():
            if not value:
                continue
            # Escape single quotes to prevent malformed filter expressions.
            escaped = str(value).replace("'", "''")
            parts.append(f"{field}='{escaped}'")
        return " and ".join(parts) if parts else None

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def embed_text(self, text: str) -> List[float]:
        """Generate an embedding vector for a single text string."""
        response = await self._provider.embed(
            input=[text],
            model=self._embedding_model,
        )
        item = response.data[0]
        # LiteLLM may return Embedding objects (attribute access) or plain dicts
        # depending on version — handle both shapes.
        if isinstance(item, dict):
            return item["embedding"]
        return item.embedding

    async def add_embedding(
        self,
        content_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Generate and upsert an embedding for `text`, keyed by `content_id`."""
        if not self._is_ready():
            return
        metadata = metadata or {}
        try:
            embedding = await self.embed_text(text)
            # Validate dimension matches schema to avoid silent mismatches.
            if len(embedding) != self._dim:
                logger.warning(
                    "Embedding dimension mismatch: expected {} got {} for content_id={!r} — skipping",
                    self._dim, len(embedding), content_id,
                )
                return

            def _upsert() -> None:
                import zvec

                collection = self._get_or_create_collection_sync()
                doc = zvec.Doc(
                    id=content_id,
                    fields={
                        "type": metadata.get("type", ""),
                        "workspace_key": metadata.get("workspace_key", ""),
                        "metadata_json": json.dumps(metadata),
                    },
                    vectors={"embedding": embedding},
                )
                collection.upsert(doc)
                logger.debug(f"Embedding upserted for content_id={content_id!r}")

            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, _upsert),
                    timeout=_EXEC_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "zvec upsert timed out after {}s for content_id={!r}",
                    _EXEC_TIMEOUT, content_id,
                )
                return
            except Exception as inner_e:
                if self._is_corruption_error(inner_e):
                    logger.warning(f"zvec corruption detected during upsert — rebuilding index: {inner_e}")
                    await loop.run_in_executor(None, self._reset_collection_sync)
                    # Retry once with the fresh collection
                    await asyncio.wait_for(
                        loop.run_in_executor(None, _upsert),
                        timeout=_EXEC_TIMEOUT,
                    )
                else:
                    raise
        except Exception as e:
            logger.warning(f"add_embedding failed for {content_id!r}: {e}")

    async def semantic_search(
        self,
        query: str,
        k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """Return the top-k most similar items to `query`.

        Returns list of (content_id, cosine_score, metadata) tuples,
        descending by score.
        """
        if not self._is_ready():
            raise RuntimeError("zvec not available")

        query_embedding = await self.embed_text(query)

        def _search() -> List[Tuple[str, float, Dict[str, Any]]]:
            import zvec

            collection = self._get_or_create_collection_sync()

            filter_str = ZvecManager._build_filter_string(filters)

            query_obj = zvec.VectorQuery("embedding", vector=query_embedding)
            kwargs: Dict[str, Any] = {"vectors": query_obj, "topk": k}
            if filter_str:
                kwargs["filter"] = filter_str

            docs = collection.query(**kwargs)

            results: List[Tuple[str, float, Dict[str, Any]]] = []
            for doc in docs:
                try:
                    meta = json.loads(doc.field("metadata_json") or "{}")
                except Exception:
                    meta = {}
                # doc.score is a float attribute; validate it is finite.
                raw_score = doc.score
                score = float(raw_score) if raw_score is not None else 0.0
                if not math.isfinite(score):
                    score = 0.0
                results.append((doc.id, score, meta))
            return results

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _search),
                timeout=_EXEC_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error("zvec search timed out after {}s for query={!r}", _EXEC_TIMEOUT, query[:80])
            raise RuntimeError(f"zvec search timed out after {_EXEC_TIMEOUT}s")
        except Exception as search_e:
            if self._is_corruption_error(search_e):
                logger.warning(f"zvec corruption detected during search — rebuilding index: {search_e}")
                await loop.run_in_executor(None, self._reset_collection_sync)
                # Retry once with the fresh collection
                return await asyncio.wait_for(
                    loop.run_in_executor(None, _search),
                    timeout=_EXEC_TIMEOUT,
                )
            raise

    def _close_sync(self) -> None:
        """Delete the collection handle so the C++ destructor flushes pending writes."""
        import gc

        with self._init_lock:
            if self._collection is not None:
                try:
                    del self._collection
                    gc.collect()
                except Exception as e:
                    logger.warning(f"zvec close error: {e}")
                finally:
                    self._collection = None

    async def close(self) -> None:
        """Flush pending zvec writes by releasing the collection handle."""
        if self._collection is None:
            logger.debug("ZvecManager closed (no collection open).")
            return
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._close_sync),
                timeout=10.0,
            )
        except Exception as e:
            logger.warning(f"ZvecManager close error: {e}")
        logger.debug("ZvecManager closed.")
