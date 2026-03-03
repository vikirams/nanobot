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
import hmac
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
        app.router.add_get("/export/latest", self._handle_export_latest)
        app.router.add_options("/export/latest", self._handle_options)
        app.router.add_post("/upload/csv", self._handle_upload_csv)
        app.router.add_options("/upload/csv", self._handle_options)
        app.router.add_get("/api/sessions", self._handle_list_sessions)
        app.router.add_options("/api/sessions", self._handle_options)
        app.router.add_get("/api/sessions/{session_id}/messages", self._handle_session_messages)
        app.router.add_options("/api/sessions/{session_id}/messages", self._handle_options)
        app.router.add_get("/api/preview/latest", self._handle_preview_latest)
        app.router.add_options("/api/preview/latest", self._handle_options)
        return app

    def _cors_headers(self) -> dict[str, str]:
        """Build CORS headers from config. Empty cors_origins = same-origin only (no wildcard)."""
        base = {
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Expose-Headers": "Content-Disposition",
            "Access-Control-Max-Age": "86400",
        }
        if self.config.cors_origins:
            base["Access-Control-Allow-Origin"] = ", ".join(self.config.cors_origins)
        return base

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
        filepath.unlink(missing_ok=True)
        return web.Response(
            status=200,
            body=data,
            content_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **self._cors_headers(),
            },
        )

    async def _handle_export_latest(self, request: Any) -> Any:
        """Serve the latest (or nth) discovery result as a CSV download.

        Query params:
          session_id  — required; the frontend session UUID
          account_id  — optional; scopes to per-account DB
          which       — 'last' (default) or 1-based index
        """
        import csv
        import io
        from datetime import datetime

        from aiohttp import web

        from nanobot.hybrid_memory.sqlite_manager import (
            SqliteManager,
            get_account_db_path,
            get_workspace_db_path,
        )
        from nanobot.agent.tools.discovery import _rows_from_payload

        # --- Auth -----------------------------------------------------------
        api_key_val = self.config.api_key.get_secret_value()
        if api_key_val:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if not hmac.compare_digest(token, api_key_val):
                return web.Response(
                    status=401,
                    content_type="application/json",
                    text=json.dumps({"error": "Unauthorized"}),
                    headers=self._cors_headers(),
                )

        # --- Params ---------------------------------------------------------
        session_id = request.rel_url.query.get("session_id", "").strip()
        account_id = request.rel_url.query.get("account_id", "").strip()
        which = request.rel_url.query.get("which", "last").strip().lower()

        if not session_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "session_id is required"}),
                headers=self._cors_headers(),
            )

        if which != "last" and not (which.isdigit() and int(which) >= 1):
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "which must be 'last' or a positive integer"}),
                headers=self._cors_headers(),
            )

        # Session key matches the format used by WebUIChannel when publishing messages.
        session_key = f"webui:{session_id}"

        # --- DB path --------------------------------------------------------
        db_path = (
            get_account_db_path(account_id)
            if account_id
            else get_workspace_db_path(self.workspace_path)
        )
        if not db_path.exists():
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "No data found for this session"}),
                headers=self._cors_headers(),
            )

        # --- Query via SqliteManager (single source of truth for discovery_results) ---
        which_idx: str | int = "last" if which == "last" else int(which)
        sqlite = SqliteManager(self.workspace_path, db_path=db_path)
        try:
            result_row = await sqlite.get_discovery_result(session_key, which_idx)
        except Exception as exc:
            logger.error("export_latest: DB error for session {}: {}", session_id, exc)
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": "Database error"}),
                headers=self._cors_headers(),
            )
        finally:
            await sqlite.close()

        if not result_row:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "No discovery results found for this session"}),
                headers=self._cors_headers(),
            )

        # --- Build CSV ------------------------------------------------------
        _, payload_str, _ = result_row
        rows = _rows_from_payload(payload_str)
        if not rows:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "Discovery result contains no rows"}),
                headers=self._cors_headers(),
            )

        all_keys: dict = {}
        for r in rows:
            all_keys.update(dict.fromkeys(r.keys()))
        columns = list(all_keys)

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({col: ("" if r.get(col) is None else r.get(col)) for col in columns})
        csv_bytes = buf.getvalue().encode("utf-8")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"discovery_{ts}.csv"
        logger.debug(
            "export_latest: serving {} rows as {} for session {}",
            len(rows), filename, session_id,
        )
        return web.Response(
            status=200,
            body=csv_bytes,
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **self._cors_headers(),
            },
        )

    async def _handle_upload_csv(self, request: Any) -> Any:
        """Accept a CSV file upload, save to workspace, and extract domain list.

        Multipart form fields:
          file       — CSV file (required)
          account_id — optional tenant scoping

        Response JSON:
          {
            "filename": "upload_20260228_123456.csv",
            "row_count": 500,
            "domain_column": "domain",   # null if no domain column detected
            "domains": ["acme.com", …],  # all extracted domain values
            "preview": ["acme.com", …],  # first 10 for display
            "columns": ["domain", "name", …]
          }
        """
        import csv
        import io
        from datetime import datetime

        from aiohttp import web

        api_key_val = self.config.api_key.get_secret_value()
        if api_key_val:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if not hmac.compare_digest(token, api_key_val):
                return web.Response(
                    status=401,
                    content_type="application/json",
                    text=json.dumps({"error": "Unauthorized"}),
                    headers=self._cors_headers(),
                )

        try:
            reader = await request.multipart()
        except Exception:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "Expected multipart/form-data"}),
                headers=self._cors_headers(),
            )

        file_bytes: bytes | None = None
        account_id = ""

        async for field in reader:
            if field.name == "file":
                file_bytes = await field.read(decode=False)
            elif field.name == "account_id":
                raw = await field.read(decode=True)
                account_id = (raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw).strip()

        if not file_bytes:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "No CSV file provided (field name: 'file')"}),
                headers=self._cors_headers(),
            )

        # Save to workspace
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"upload_{ts}.csv"
        dest = self.workspace_path / filename
        try:
            dest.write_bytes(file_bytes)
        except Exception as exc:
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": f"Could not save file: {exc}"}),
                headers=self._cors_headers(),
            )

        # Parse CSV and detect domain column
        text = file_bytes.decode("utf-8-sig", errors="replace")  # strip BOM if present
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        reader_csv = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows: list[dict] = []
        try:
            for row in reader_csv:
                rows.append(row)
        except Exception:
            pass

        columns = list(reader_csv.fieldnames or [])

        # Detect domain column: exact names first, then heuristic
        _DOMAIN_NAMES = {"domain", "website", "url", "company_url", "company_domain",
                         "site", "company_website", "homepage"}
        domain_col: str | None = None
        for col in columns:
            if col.lower().strip() in _DOMAIN_NAMES:
                domain_col = col
                break
        # Fallback: first column whose first value looks like a domain
        if not domain_col and rows:
            first_row = rows[0]
            for col, val in first_row.items():
                val = str(val or "").strip()
                if val and "." in val and " " not in val and not val.startswith("http"):
                    domain_col = col
                    break

        # Extract and normalise domains
        domains: list[str] = []
        if domain_col:
            for row in rows:
                raw_val = str(row.get(domain_col) or "").strip()
                if not raw_val:
                    continue
                # Normalise: strip scheme, www, trailing slashes
                raw_val = raw_val.lower()
                for prefix in ("https://", "http://", "www."):
                    if raw_val.startswith(prefix):
                        raw_val = raw_val[len(prefix):]
                raw_val = raw_val.rstrip("/").split("/")[0]
                if raw_val and "." in raw_val:
                    domains.append(raw_val)

        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({
                "filename": filename,
                "row_count": len(rows),
                "domain_column": domain_col,
                "domains": domains,
                "preview": domains[:10],
                "columns": columns,
            }, ensure_ascii=False),
            headers=self._cors_headers(),
        )

    async def _handle_preview_latest(self, request: Any) -> Any:
        """Return the latest discovery result as JSON for client-side table rendering.

        Query params:
          session_id  — required; the frontend session UUID
          account_id  — optional; scopes to per-account DB
          which       — 'last' (default) or 1-based index
          max_rows    — max rows to return (default 20, max 100)

        Response JSON:
          {"columns": [...], "rows": [...], "total": N, "preview_rows": M}
        """
        from aiohttp import web

        from nanobot.hybrid_memory.sqlite_manager import (
            SqliteManager,
            get_account_db_path,
            get_workspace_db_path,
        )
        from nanobot.agent.tools.discovery import _rows_from_payload

        api_key_val = self.config.api_key.get_secret_value()
        if api_key_val:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if not hmac.compare_digest(token, api_key_val):
                return web.Response(
                    status=401,
                    content_type="application/json",
                    text=json.dumps({"error": "Unauthorized"}),
                    headers=self._cors_headers(),
                )

        session_id = request.rel_url.query.get("session_id", "").strip()
        account_id = request.rel_url.query.get("account_id", "").strip()
        which = request.rel_url.query.get("which", "last").strip().lower()
        try:
            max_rows = min(int(request.rel_url.query.get("max_rows", "20")), 100)
        except ValueError:
            max_rows = 20

        if not session_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "session_id is required"}),
                headers=self._cors_headers(),
            )

        session_key = f"webui:{session_id}"
        db_path = (
            get_account_db_path(account_id)
            if account_id
            else get_workspace_db_path(self.workspace_path)
        )

        if not db_path.exists():
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "No data found for this session"}),
                headers=self._cors_headers(),
            )

        which_idx: str | int = "last" if which == "last" else int(which)
        sqlite = SqliteManager(self.workspace_path, db_path=db_path)
        try:
            result_row = await sqlite.get_discovery_result(session_key, which_idx)
        except Exception as exc:
            logger.error("preview_latest: DB error for session {}: {}", session_id, exc)
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": "Database error"}),
                headers=self._cors_headers(),
            )
        finally:
            await sqlite.close()

        if not result_row:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "No discovery results found for this session"}),
                headers=self._cors_headers(),
            )

        _, payload_str, _ = result_row
        all_rows = _rows_from_payload(payload_str)
        if not all_rows:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "Discovery result contains no rows"}),
                headers=self._cors_headers(),
            )

        total = len(all_rows)
        preview = all_rows[:max_rows]
        all_keys: dict = {}
        for r in preview:
            all_keys.update(dict.fromkeys(r.keys()))
        columns = list(all_keys)

        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(
                {
                    "columns": columns,
                    "rows": preview,
                    "total": total,
                    "preview_rows": len(preview),
                },
                ensure_ascii=False,
            ),
            headers=self._cors_headers(),
        )

    async def _handle_list_sessions(self, request: Any) -> Any:
        """List all WebUI sessions with title preview, ordered by most-recent.

        Query params:
          account_id — optional; scopes to per-account DB
        Response: JSON array of {session_id, title, updated_at}
        """
        import aiosqlite
        from aiohttp import web

        from nanobot.hybrid_memory.sqlite_manager import (
            get_account_db_path,
            get_workspace_db_path,
        )

        api_key_val = self.config.api_key.get_secret_value()
        if api_key_val:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if not hmac.compare_digest(token, api_key_val):
                return web.Response(
                    status=401,
                    content_type="application/json",
                    text=json.dumps({"error": "Unauthorized"}),
                    headers=self._cors_headers(),
                )

        account_id = request.rel_url.query.get("account_id", "").strip()
        db_path = (
            get_account_db_path(account_id)
            if account_id
            else get_workspace_db_path(self.workspace_path)
        )

        if not db_path.exists():
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps([]),
                headers=self._cors_headers(),
            )

        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute(
                    """
                    SELECT
                        m.session_id,
                        COALESCE(sm.updated_at, MAX(m.timestamp)) AS updated_at,
                        (SELECT m2.content FROM messages m2
                         WHERE m2.session_id = m.session_id AND m2.role = 'user'
                         ORDER BY m2.timestamp ASC LIMIT 1) AS title
                    FROM messages m
                    LEFT JOIN session_metadata sm ON sm.session_id = m.session_id
                    WHERE m.session_id LIKE 'webui:%'
                    GROUP BY m.session_id
                    ORDER BY updated_at DESC
                    LIMIT 50
                    """
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            logger.error("list_sessions DB error: {}", exc)
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": "Database error"}),
                headers=self._cors_headers(),
            )

        sessions = []
        for row in rows:
            full_key = row[0]  # "webui:<uuid>"
            sid = full_key[len("webui:"):]
            title = (row[2] or "New conversation")[:100]
            sessions.append({"session_id": sid, "title": title, "updated_at": row[1]})

        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(sessions, ensure_ascii=False),
            headers=self._cors_headers(),
        )

    async def _handle_session_messages(self, request: Any) -> Any:
        """Return the message history for a WebUI session (for UI rendering on session switch).

        Path param:
          session_id — the bare UUID (without 'webui:' prefix)
        Query params:
          account_id — optional; scopes to per-account DB
        Response: JSON array of {role, content, timestamp}
        """
        import aiosqlite
        from aiohttp import web

        from nanobot.hybrid_memory.sqlite_manager import (
            get_account_db_path,
            get_workspace_db_path,
        )

        api_key_val = self.config.api_key.get_secret_value()
        if api_key_val:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if not hmac.compare_digest(token, api_key_val):
                return web.Response(
                    status=401,
                    content_type="application/json",
                    text=json.dumps({"error": "Unauthorized"}),
                    headers=self._cors_headers(),
                )

        raw_session_id = request.match_info["session_id"].strip()
        account_id = request.rel_url.query.get("account_id", "").strip()

        if not raw_session_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "session_id required"}),
                headers=self._cors_headers(),
            )

        session_key = f"webui:{raw_session_id}"
        db_path = (
            get_account_db_path(account_id)
            if account_id
            else get_workspace_db_path(self.workspace_path)
        )

        if not db_path.exists():
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps([]),
                headers=self._cors_headers(),
            )

        try:
            async with aiosqlite.connect(db_path) as conn:
                cursor = await conn.execute(
                    """
                    SELECT role, content, timestamp FROM messages
                    WHERE session_id = ?
                      AND role IN ('user', 'assistant')
                      AND content IS NOT NULL AND content != ''
                    ORDER BY timestamp ASC
                    LIMIT 200
                    """,
                    (session_key,),
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            logger.error("session_messages DB error for {}: {}", raw_session_id, exc)
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": "Database error"}),
                headers=self._cors_headers(),
            )

        messages = [{"role": r[0], "content": r[1] or "", "timestamp": r[2]} for r in rows]
        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(messages, ensure_ascii=False),
            headers=self._cors_headers(),
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
        api_key_val = self.config.api_key.get_secret_value()
        if api_key_val:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.removeprefix("Bearer ").strip()
            if not hmac.compare_digest(token, api_key_val):
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

        # Accept both camelCase (sessionId, accountId) and snake_case variants
        # so existing clients don't need to change their payload format.
        session_id: str = (
            body.get("session_id") or body.get("sessionId") or str(uuid.uuid4())
        )
        # account_id scopes the workspace-level memory and storage to a specific tenant.
        # Clients pass this to isolate different accounts sharing one nanobot instance.
        # If absent, all sessions share the same workspace (original behaviour).
        account_id: str = (body.get("account_id") or body.get("accountId") or "").strip()
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
            queue: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=1000)
            self._queues[chat_id] = queue

            try:
                # Publish user message to the agent bus
                metadata: dict = {"session_id": session_id}
                if account_id:
                    metadata["account_id"] = account_id
                await self._handle_message(
                    sender_id="user",
                    chat_id=chat_id,
                    content=content,
                    metadata=metadata,
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
                    is_streaming = msg.metadata.get("_streaming", False)

                    if is_progress:
                        if is_streaming:
                            # Individual token delta — emit as 'token' event so the
                            # frontend can append it to a streaming buffer.
                            await self._write_sse(
                                response,
                                event="token",
                                data={"type": "token", "content": msg.content},
                            )
                        else:
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

            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, OSError):
                logger.debug("WebUI: client disconnected for session {}, sending stop signal", session_id)
                try:
                    stop_meta: dict = {"session_id": session_id}
                    if account_id:
                        stop_meta["account_id"] = account_id
                    await self._handle_message(
                        sender_id="user",
                        chat_id=chat_id,
                        content="/stop",
                        metadata=stop_meta,
                    )
                except Exception:
                    pass
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
