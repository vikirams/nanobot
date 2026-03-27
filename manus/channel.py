"""Web channel implementation for real-time agent interaction."""

import asyncio
import json
from datetime import datetime
from typing import Any, AsyncGenerator

try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
    import uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    # Define dummy classes to avoid NameError
    class BaseModel: pass
    class FastAPI:
        def __init__(self, *args, **kwargs): pass
        def add_middleware(self, *args, **kwargs): pass
        def post(self, *args, **kwargs): return lambda x: x
        def get(self, *args, **kwargs): return lambda x: x

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel

if FASTAPI_AVAILABLE:
    class MessageRequest(BaseModel):
        content: str
        chat_id: str = "default"
        sender_id: str = "user"
        metadata: dict[str, Any] = {}

class ManusWebChannel(BaseChannel):
    """
    Web channel that provides a REST API and SSE streaming for agent interaction.
    """

    name = "web"

    def __init__(self, config, bus: MessageBus, session_manager=None):
        super().__init__(config, bus)
        self.session_manager = session_manager
        if FASTAPI_AVAILABLE:
            self.app = FastAPI(title="nanobot Web API (Manus Extension)")
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=getattr(config, "cors_origins", ["*"]),
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
            self._setup_routes()
        else:
            self.app = None

        self._event_queues: dict[str, list[asyncio.Queue]] = {}
        self._server_task = None

    def _setup_routes(self):
        @self.app.post("/api/messages")
        async def send_message(req: MessageRequest):
            await self._handle_message(
                sender_id=req.sender_id,
                chat_id=req.chat_id,
                content=req.content,
                metadata=req.metadata
            )
            return {"status": "sent", "chat_id": req.chat_id}

        @self.app.get("/api/events/{chat_id}")
        async def stream_events(chat_id: str):
            return StreamingResponse(
                self._event_generator(chat_id),
                media_type="text/event-stream"
            )

        @self.app.get("/api/sessions/{session_id}")
        async def get_session(session_id: str):
            if not self.session_manager:
                return {"error": "Session manager not available"}
            session = self.session_manager.get_or_create(f"web:{session_id}")
            return {
                "session_id": session_id,
                "messages": session.messages,
                "metadata": session.metadata
            }

        @self.app.get("/api/health")
        async def health():
            return {"status": "ok"}

    async def _event_generator(self, chat_id: str) -> AsyncGenerator[str, None]:
        queue = asyncio.Queue()
        self._event_queues.setdefault(chat_id, []).append(queue)
        logger.info(f"New SSE subscriber for chat_id: {chat_id}")
        try:
            yield f"data: {json.dumps({'event_type': 'connected', 'chat_id': chat_id})}\n\n"

            while True:
                msg: OutboundMessage = await queue.get()
                # Extract event_type from metadata if possible, otherwise default to "message"
                event_type = msg.metadata.get("event_type", "message")
                timestamp = msg.metadata.get("timestamp") or datetime.now().isoformat()

                data = {
                    "event_type": event_type,
                    "content": msg.content,
                    "metadata": msg.metadata,
                    "timestamp": timestamp,
                    "chat_id": msg.chat_id
                }
                yield f"data: {json.dumps(data)}\n\n"
        except asyncio.CancelledError:
            logger.info(f"SSE subscriber disconnected for chat_id: {chat_id}")
            raise
        except Exception as e:
            logger.error(f"Error in SSE generator: {e}")
        finally:
            if chat_id in self._event_queues:
                if queue in self._event_queues[chat_id]:
                    self._event_queues[chat_id].remove(queue)
                if not self._event_queues[chat_id]:
                    del self._event_queues[chat_id]

    async def start(self) -> None:
        if not FASTAPI_AVAILABLE:
            logger.error("FastAPI or Uvicorn not installed. Please run 'pip install fastapi uvicorn'")
            return

        self._running = True
        config = uvicorn.Config(
            self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="info",
            access_log=False
        )
        server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(server.serve())
        logger.info(f"Web channel started on http://{self.config.host}:{self.config.port}")

    async def stop(self) -> None:
        self._running = False
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        logger.info("Web channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Forward outbound messages to SSE subscribers."""
        if msg.chat_id in self._event_queues:
            for queue in self._event_queues[msg.chat_id]:
                await queue.put(msg)

        if "*" in self._event_queues:
            for queue in self._event_queues["*"]:
                await queue.put(msg)
