"""PostgreSQL connection pool management."""
from __future__ import annotations

import asyncio
import re
import ssl
from typing import Optional
from urllib.parse import urlparse, urlunparse

import asyncpg
from loguru import logger

_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


def build_dsn(url: str, user: str = "", password: str = "") -> str:
    """
    Inject user/password credentials into a DSN that may omit them.

    Allows config.json (Secrets Manager) to store only the host/dbname:
        postgresql://host:5432/nanobot
    while credentials come from env vars NANOBOT_DATABASE__USER / __PASSWORD.

    If user/password are already in the URL they are left unchanged (env
    override only applies when the URL has no userinfo component).
    """
    if not (user or password):
        return url

    parsed = urlparse(url)
    # Only inject when the URL has no credentials already
    if not parsed.username and not parsed.password:
        netloc = f"{user}:{password}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        parsed = parsed._replace(netloc=netloc)

    return urlunparse(parsed)


def _strip_sslmode(dsn: str) -> tuple[str, Optional[ssl.SSLContext]]:
    """
    Remove sslmode=... from DSN and return an SSL context for asyncpg.

    AWS RDS uses its own CA which is not in Python's default bundle.
    asyncpg ignores sslmode in DSN — we must pass ssl= explicitly.
    """
    if "sslmode=" not in dsn:
        return dsn, None

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    clean = re.sub(
        r"[?&]sslmode=[^&]*",
        lambda m: "?" if m.group().startswith("?") else "",
        dsn,
    ).rstrip("?").rstrip("&")

    return clean, ctx


async def get_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 10,
    command_timeout: int = 60,
    user: str = "",
    password: str = "",
) -> asyncpg.Pool:
    """Get or create the shared asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is not None:
            return _pool

        dsn = build_dsn(dsn, user, password)
        dsn, ssl_ctx = _strip_sslmode(dsn)

        logger.info("Creating PostgreSQL connection pool")
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=command_timeout,
            **({"ssl": ssl_ctx} if ssl_ctx is not None else {}),
        )

        # Enable pgvector if available — non-fatal if extension not installed
        async with _pool.acquire() as conn:
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                logger.info("pgvector extension ready")
            except Exception as e:
                logger.warning("pgvector not available — semantic memory disabled: {}", e)

        logger.info("PostgreSQL pool ready (min={}, max={})", min_size, max_size)
        return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL pool closed")
