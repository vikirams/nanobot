"""Web UI channel — SSE endpoint for React (or any) frontends.

Protocol
--------
POST /chat
  Request headers:
    Content-Type: application/json
    Authorization: Bearer <api_key>   (only if api_key is configured)
  Request body:
    {"content": "<user message>", "session_id": "<uuid>"}
  Response: text/event-stream
    event: progress
    data: {"type": "progress", "content": "<tool hint or interim text>"}

    event: final
    data: {"type": "final", "content": "<full assistant reply>"}

    : keepalive          (comment line, sent every 15 s to prevent proxy timeouts)

GET /health
  Response: 200 {"status": "ok"}

GET /download/{filename}
  Serves a CSV file from the workspace directory (~/.nanobot/workspace/).
  Response: application/octet-stream with Content-Disposition: attachment

OPTIONS /chat
  CORS preflight — handled automatically.

Session isolation
-----------------
Each unique session_id maps to an independent conversation in the agent.
The session key written to the SessionManager is "webui:<session_id>".
The frontend should persist session_id in localStorage to maintain history
across page refreshes. Sending a new UUID starts a fresh conversation.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


class WebUIChannel(BaseChannel):
    """Chat channel that exposes an SSE HTTP endpoint for web frontends."""

    name = "webui"

    # How long (seconds) to wait for the agent to produce the final response
    # before sending a keepalive comment to the client.
    _KEEPALIVE_INTERVAL = 15.0

    # Maximum time (seconds) to wait for the final response before timing out.
    _RESPONSE_TIMEOUT = 300.0

    def __init__(self, config: Any, bus: MessageBus, workspace_path: Path | None = None) -> None:
        super().__init__(config, bus)
        self.workspace_path = workspace_path or Path(os.path.expanduser("~/.nanobot/workspace"))
        # chat_id → asyncio.Queue[OutboundMessage]
        # Each active SSE request registers its own queue here.
        self._queues: dict[str, asyncio.Queue[OutboundMessage]] = {}
        # Per-session lock: prevents a new request starting before the
        # previous turn's response has been fully delivered.
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._runner: Any = None  # aiohttp AppRunner

    # ------------------------------------------------------------------
    # BaseChannel lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the aiohttp HTTP server and keep it running."""
        from aiohttp import web

        self._running = True
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await site.start()
        logger.info(
            "WebUI channel listening on http://{}:{}", self.config.host, self.config.port
        )

        # Keep running until stopped — aiohttp runs in the same event loop.
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Gracefully shut down the HTTP server."""
        self._running = False
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("WebUI channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Route an outbound message into the waiting SSE request's queue."""
        queue = self._queues.get(msg.chat_id)
        if queue is None:
            # No active SSE connection for this chat_id (e.g. client disconnected).
            logger.debug("WebUI: no active queue for session {}, dropping message", msg.chat_id)
            return
        await queue.put(msg)

    # ------------------------------------------------------------------
    # aiohttp app / route builders
    # ------------------------------------------------------------------

    def _build_app(self) -> Any:
        from aiohttp import web

        app = web.Application()
        app.router.add_post("/chat", self._handle_chat)
        app.router.add_options("/chat", self._handle_options)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/download/{filename}", self._handle_download)
        app.router.add_options("/download/{filename}", self._handle_options)
        return app

    def _cors_headers(self) -> dict[str, str]:
        """Build CORS headers from config."""
        origin = ", ".join(self.config.cors_origins) if self.config.cors_origins else "*"
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Expose-Headers": "Content-Disposition",
            "Access-Control-Max-Age": "86400",
        }

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_options(self, request: Any) -> Any:
        """CORS preflight handler."""
        from aiohttp import web
        return web.Response(status=204, headers=self._cors_headers())

    async def _handle_health(self, request: Any) -> Any:
        """Liveness probe — React can poll this before showing the chat UI."""
        from aiohttp import web
        return web.Response(
            content_type="application/json",
            text=json.dumps({"status": "ok", "workspace": str(self.workspace_path)}),
            headers=self._cors_headers(),
        )

    async def _handle_download(self, request: Any) -> Any:
        """Serve a CSV file from the workspace directory."""
        from aiohttp import web

        filename = request.match_info["filename"]
        # Prevent path traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "Invalid filename"}),
                headers=self._cors_headers(),
            )

        filepath = self.workspace_path / filename
        logger.debug("WebUI download: looking for {} in {}", filename, self.workspace_path)
        if not filepath.exists() or not filepath.is_file():
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({
                    "error": "File not found",
                    "looked_in": str(filepath),
                }),
                headers=self._cors_headers(),
            )

        data = filepath.read_bytes()
        return web.Response(
            status=200,
            body=data,
            content_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **self._cors_headers(),
            },
        )

    async def _handle_chat(self, request: Any) -> Any:
        """
        Main SSE endpoint.

        1. Validate auth (if configured).
        2. Parse body for content + session_id.
        3. Acquire per-session lock (serialises turns within one session).
        4. Register a response queue mapped to this session's chat_id.
        5. Publish InboundMessage to the bus.
        6. Stream OutboundMessages back as SSE events until 'final' arrives.
        7. Release lock and clean up queue.
        """
        from aiohttp import web

        # --- Auth -------------------------------------------------------
        if self.config.api_key:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if token != self.config.api_key:
                return web.Response(
                    status=401,
                    content_type="application/json",
                    text=json.dumps({"error": "Unauthorized"}),
                    headers=self._cors_headers(),
                )

        # --- Parse body -------------------------------------------------
        try:
            body = await request.json()
        except Exception:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "Invalid JSON body"}),
                headers=self._cors_headers(),
            )

        content: str = body.get("content", "").strip()
        if not content:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "'content' field is required"}),
                headers=self._cors_headers(),
            )

        session_id: str = body.get("session_id") or str(uuid.uuid4())
        chat_id = session_id  # used as the bus routing key

        # --- Per-session lock (serialise turns) -------------------------
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        lock = self._session_locks[session_id]

        # --- Prepare SSE response stream --------------------------------
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",   # disable Nginx buffering
                **self._cors_headers(),
            },
        )
        await response.prepare(request)

        async with lock:
            # Register queue for this chat_id
            queue: asyncio.Queue[OutboundMessage] = asyncio.Queue()
            self._queues[chat_id] = queue

            try:
                # Publish user message to the agent bus
                await self._handle_message(
                    sender_id="user",
                    chat_id=chat_id,
                    content=content,
                    metadata={"session_id": session_id},
                )

                # Drain queue: stream events until 'final' received
                elapsed = 0.0
                while elapsed < self._RESPONSE_TIMEOUT:
                    try:
                        msg = await asyncio.wait_for(
                            queue.get(), timeout=self._KEEPALIVE_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        # Send keepalive comment to prevent proxy/CDN timeout
                        await self._write_sse(response, comment="keepalive")
                        elapsed += self._KEEPALIVE_INTERVAL
                        continue

                    is_progress = msg.metadata.get("_progress", False)

                    if is_progress:
                        await self._write_sse(
                            response,
                            event="progress",
                            data={"type": "progress", "content": msg.content},
                        )
                    else:
                        # Final response — send and close stream
                        await self._write_sse(
                            response,
                            event="final",
                            data={"type": "final", "content": msg.content},
                        )
                        break
                else:
                    # Timeout: send an error event so the client knows
                    await self._write_sse(
                        response,
                        event="error",
                        data={"type": "error", "content": "Response timed out"},
                    )

            except ConnectionResetError:
                logger.debug("WebUI: client disconnected for session {}", session_id)
            except Exception as e:
                logger.error("WebUI: error streaming session {}: {}", session_id, e)
                try:
                    await self._write_sse(
                        response,
                        event="error",
                        data={"type": "error", "content": f"Internal error: {e}"},
                    )
                except Exception:
                    pass
            finally:
                self._queues.pop(chat_id, None)

        return response

    # ------------------------------------------------------------------
    # SSE helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _write_sse(
        response: Any,
        *,
        event: str | None = None,
        data: dict | None = None,
        comment: str | None = None,
    ) -> None:
        """Write a single SSE frame to the response stream.

        SSE wire format:
            : comment\\n\\n
            event: <name>\\ndata: <json>\\n\\n
        """
        if comment is not None:
            chunk = f": {comment}\n\n"
        else:
            lines = []
            if event:
                lines.append(f"event: {event}")
            if data is not None:
                lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
            chunk = "\n".join(lines) + "\n\n"

        await response.write(chunk.encode("utf-8"))
