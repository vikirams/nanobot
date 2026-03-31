"""Web UI channel — SSE endpoint for React (or any) frontends.

Core
----
POST /chat
  Request headers:
    Content-Type: application/json
    Authorization: Bearer <api_key>   (only if api_key is configured)
  Request body:
    {"content": "<user message>", "session_id": "<uuid>"}
  Response: text/event-stream
    event: token     — streaming token (when provider supports it)
    event: progress  — tool hint or interim progress message
    event: final     — full assistant reply
    : keepalive      — comment line sent every 15 s to prevent proxy timeouts

GET /health
  Response: 200 {"status": "ok", "workspace": "<path>"}

GET /download/{filename}
  Serves a file from the workspace directory (~/.nanobot/workspace/).
  Response: application/octet-stream with Content-Disposition: attachment

Discovery / Export
------------------
GET /export/latest?session_id=&account_id=&user_id=&which=last
  Exports the latest MCP discovery result for the session as a CSV download.
  which: 'last' (default) or 1-based index into stored resultsets.

GET /api/preview/latest?session_id=&account_id=&user_id=&which=last&max_rows=20
  Returns the latest discovery result as JSON for in-UI table rendering.
  Response: {"columns": [...], "rows": [...], "total": N, "preview_rows": M}

POST /upload/csv
  Accepts a multipart CSV file upload. Parses the file, detects the domain
  column, and returns row count, column list, and extracted domains.

Session Management
------------------
GET /api/sessions?account_id=
  Lists all WebUI sessions for the account ordered by last activity.
  Response: [{"session_id": ..., "title": ..., "updated_at": ...}, ...]

GET /api/sessions/{session_id}/messages?account_id=
  Returns user + assistant message history for a session (up to 200 messages).

MCP Integration
---------------
GET /api/mcp/prompts?account_id=
  Lists available MCP prompts for slash command autocomplete.
  Response: {"prompts": [{"name": ..., "description": ..., "arguments": [...]}, ...]}

POST /api/mcp/prompt
  Executes an MCP prompt (slash command) and streams the result via SSE.
  Request body: {"prompt_name": ..., "arguments": {...}, "account_id": ...}

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
import re
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


_STRIP_TOP = frozenset(["id", "profilePicture", "sources"])
_STRIP_NESTED = frozenset(["id", "companyLogo"])


def _flatten_preview_record(record: dict) -> dict:
    """Generic structural flattener — works on contacts, segments, custom-field rows, etc.

    Rules:
      - Nested plain object  → lift all children to top level
                               (company.companyName → companyName,
                                customFields.revenue → revenue)
      - Array of primitives  → first scalar value kept under the original key
      - Array of objects     → skipped (conditions, columnInfo, etc.)
      - Primitive / null     → kept as-is
    Stripped (display noise): id, profilePicture, sources at top level;
                               id, companyLogo inside nested objects.
    """
    if not isinstance(record, dict):
        return record

    flat: dict = {}

    for key, value in record.items():
        if key in _STRIP_TOP:
            continue

        if value is None:
            flat[key] = None

        elif isinstance(value, list):
            if not value:
                flat[key] = None
            elif not isinstance(value[0], dict):
                # Array of primitives → first scalar
                flat[key] = value[0]
            # Array of objects → skip

        elif isinstance(value, dict):
            # Nested object → lift children one level up
            for sub_key, sub_value in value.items():
                if sub_key in _STRIP_NESTED:
                    continue
                if sub_value is None or not isinstance(sub_value, (dict, list)):
                    flat[sub_key] = sub_value
                elif isinstance(sub_value, list):
                    if sub_value and not isinstance(sub_value[0], dict):
                        flat[sub_key] = sub_value[0]
                    # Nested array of objects → skip
                # Doubly-nested object → skip

        else:
            flat[key] = value

    return flat


def _rows_from_payload(payload: str) -> list[dict]:
    """Extract a list of row dicts from a JSON discovery payload."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("results", "data", "items", "records", "preview"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
    return []


_ICP_PATTERN = re.compile(
    r"\b(my|our)\s+icp\b"
    r"|\b(my|our)\s+personas?\b"
    r"|\b(my|our)\s+target\s+(audience|customers?|personas?)\b",
    re.IGNORECASE,
)


def _rewrite_icp_with_dna(query: str, dna: dict) -> str:
    """Replace 'my ICP'/'our ICP'/'our personas' with actual persona titles from DNA.

    Supports both rich nested format (Snowflake-style: contact_icp[].target_titles[])
    and flat format (icp.titles or icp.personas).
    """
    if not _ICP_PATTERN.search(query):
        return query

    titles: list[str] = []
    # Rich nested format
    for cicp in (dna.get("contact_icp") or []):
        for t in (cicp.get("target_titles") or []):
            if isinstance(t, dict) and t.get("priority") == "primary":
                title = t.get("title") or t.get("name", "")
                if title:
                    titles.append(title)
    # Flat format fallback
    if not titles:
        flat_icp = dna.get("icp") or {}
        for t in (flat_icp.get("titles") or flat_icp.get("personas") or []):
            if isinstance(t, str) and t:
                titles.append(t)

    if not titles:
        return query

    replacement = ", ".join(titles[:6])
    return _ICP_PATTERN.sub(replacement, query)


class WebUIChannel(BaseChannel):
    """Chat channel that exposes an SSE HTTP endpoint for web frontends."""

    name = "webui"

    # How long (seconds) to wait for the agent to produce the final response
    # before sending a keepalive comment to the client.
    _KEEPALIVE_INTERVAL = 15.0

    # Maximum time (seconds) to wait for the final response before timing out.
    _RESPONSE_TIMEOUT = 300.0

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        workspace_path: Path | None = None,
        db_manager: Any = None,
        mcp_config: dict | None = None,
    ) -> None:
        super().__init__(config, bus)
        self.workspace_path = workspace_path or Path(os.path.expanduser("~/.nanobot/workspace"))
        # DBManager instance — injected lazily via set_agent_loop() once AgentLoop
        # finishes _ensure_db(). All DB-backed endpoints check _db_manager before use.
        self._db_manager = db_manager
        self._agent_loop: Any = None  # set by set_agent_loop()
        # chat_id → asyncio.Queue[OutboundMessage]
        # Each active SSE request registers its own queue here.
        self._queues: dict[str, asyncio.Queue[OutboundMessage]] = {}
        # Per-session lock: prevents a new request starting before the
        # previous turn's response has been fully delivered.
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._runner: Any = None  # aiohttp AppRunner
        # MCP prompt manager for slash commands
        self._mcp_manager: Any = None
        self._mcp_config = mcp_config or {}

    def set_agent_loop(self, agent_loop: Any) -> None:
        """Wire the AgentLoop so DB-backed endpoints can access its db_manager.

        Called from AgentLoop._ensure_db() after the pool is ready.
        """
        self._agent_loop = agent_loop

    @property
    def _effective_db_manager(self) -> Any:
        """Return the live DBManager, preferring the agent loop's instance."""
        if self._agent_loop is not None and self._agent_loop._db_manager is not None:
            return self._agent_loop._db_manager
        return self._db_manager

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
        app.router.add_delete("/api/sessions/{session_id}", self._handle_delete_session)
        app.router.add_options("/api/sessions/{session_id}", self._handle_options)
        app.router.add_get("/api/preview/latest", self._handle_preview_latest)
        app.router.add_options("/api/preview/latest", self._handle_options)
        # MCP slash command endpoints
        app.router.add_get("/api/mcp/prompts", self._handle_list_mcp_prompts)
        app.router.add_options("/api/mcp/prompts", self._handle_options)
        app.router.add_post("/api/mcp/prompt", self._handle_mcp_prompt)
        app.router.add_options("/api/mcp/prompt", self._handle_options)
        app.router.add_post("/api/parse-command", self._handle_parse_command)
        app.router.add_options("/api/parse-command", self._handle_options)
        app.router.add_get("/api/mcp/export", self._handle_mcp_export)
        app.router.add_options("/api/mcp/export", self._handle_options)
        app.router.add_get("/api/accounts/{account_id}/dna", self._handle_get_dna)
        app.router.add_put("/api/accounts/{account_id}/dna", self._handle_put_dna)
        app.router.add_options("/api/accounts/{account_id}/dna", self._handle_options)
        return app

    def _cors_headers(self, request: Any = None) -> dict[str, str]:
        """Build CORS headers from config.

        Access-Control-Allow-Origin must be a single value — browsers reject comma-
        joined lists. When cors_origins has multiple entries we echo back the request's
        Origin if it matches, otherwise we use the first allowed origin.
        """
        base = {
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Expose-Headers": "Content-Disposition",
            "Access-Control-Max-Age": "86400",
        }
        if not self.config.cors_origins:
            return base
        allowed = self.config.cors_origins
        # Prefer echoing back the exact requesting origin when it is in the allow-list
        request_origin = (request.headers.get("Origin", "") if request is not None else "")
        if request_origin and request_origin in allowed:
            base["Access-Control-Allow-Origin"] = request_origin
            base["Vary"] = "Origin"
        else:
            base["Access-Control-Allow-Origin"] = allowed[0]
            base["Vary"] = "Origin"
        return base

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_options(self, request: Any) -> Any:
        """CORS preflight handler."""
        from aiohttp import web
        return web.Response(status=204, headers=self._cors_headers(request))

    async def _handle_health(self, request: Any) -> Any:
        """Liveness probe — React can poll this before showing the chat UI."""
        from aiohttp import web
        return web.Response(
            content_type="application/json",
            text=json.dumps({"status": "ok", "workspace": str(self.workspace_path)}),
            headers=self._cors_headers(request),
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
                headers=self._cors_headers(request),
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
                headers=self._cors_headers(request),
            )

        data = filepath.read_bytes()
        filepath.unlink(missing_ok=True)
        return web.Response(
            status=200,
            body=data,
            content_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                **self._cors_headers(request),
            },
        )

    async def _handle_export_latest(self, request: Any) -> Any:
        """Serve the latest MCP discovery result as a CSV download.

        Query params:
          session_id  — required; the frontend session UUID
          account_id  — optional; scopes discovery to this account
          which       — 'last' (default) or 1-based index (1 = oldest)

        Fetches tool messages with a resultset_ref from session_messages (Postgres).
        """
        import csv
        import io
        from datetime import datetime

        from aiohttp import web

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
                    headers=self._cors_headers(request),
                )

        # --- Params ---------------------------------------------------------
        session_id = request.rel_url.query.get("session_id", "").strip()
        account_id = request.rel_url.query.get("account_id", "").strip()
        user_id = request.rel_url.query.get("user_id", "").strip()
        which = request.rel_url.query.get("which", "last").strip().lower()

        if not session_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "session_id is required"}),
                headers=self._cors_headers(request),
            )

        if which != "last" and not (which.isdigit() and int(which) >= 1):
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "which must be 'last' or a positive integer"}),
                headers=self._cors_headers(request),
            )

        session_key = f"webui:{session_id}"
        payload_str = await self._get_discovery_payload(session_key, account_id, user_id, which)

        if payload_str is None:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "No discovery results found for this session"}),
                headers=self._cors_headers(request),
            )

        # --- Build CSV ------------------------------------------------------
        rows = _rows_from_payload(payload_str)
        if not rows:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "Discovery result contains no rows"}),
                headers=self._cors_headers(request),
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
                **self._cors_headers(request),
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
                    headers=self._cors_headers(request),
                )

        try:
            reader = await request.multipart()
        except Exception:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "Expected multipart/form-data"}),
                headers=self._cors_headers(request),
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
                headers=self._cors_headers(request),
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
                headers=self._cors_headers(request),
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
            headers=self._cors_headers(request),
        )

    async def _get_discovery_payload(
        self, session_key: str, account_id: str, user_id: str, which: str
    ) -> str | None:
        """Retrieve the raw JSON payload of an MCP discovery result from Postgres.

        Queries session_messages for tool rows with a non-null resultset_ref, then
        returns either the last one or the nth-oldest (1-based), depending on *which*.
        Returns None if no matching rows exist or if the DB is not ready.
        """
        _db = self._effective_db_manager
        if _db is None:
            return None
        try:
            rows = await _db.get_messages_for_session(
                session_key, limit=-1, account_id=account_id, user_id=user_id
            )
        except Exception as exc:
            logger.error("_get_discovery_payload DB error for {}: {}", session_key, exc)
            return None

        # Collect tool messages that have a resultset_ref (MCP discovery results)
        discovery_rows = []
        for row in rows:
            if row.get("role") != "tool":
                continue
            if not row.get("resultset_ref"):
                continue
            content = row.get("content", {})
            if isinstance(content, dict):
                payload = json.dumps(content, ensure_ascii=False)
            elif isinstance(content, str):
                payload = content
            else:
                continue
            discovery_rows.append(payload)

        if not discovery_rows:
            return None

        if which == "last":
            return discovery_rows[-1]
        try:
            idx = int(which) - 1  # convert 1-based to 0-based
            if 0 <= idx < len(discovery_rows):
                return discovery_rows[idx]
        except (ValueError, TypeError):
            pass
        return None

    async def _handle_preview_latest(self, request: Any) -> Any:
        """Return the latest discovery result as JSON for client-side table rendering.

        Query params:
          session_id  — required; the frontend session UUID
          account_id  — optional (used for auth context, not DB routing)
          which       — 'last' (default) or 1-based index
          max_rows    — max rows to return (default 20, max 100)

        Response JSON:
          {"columns": [...], "rows": [...], "total": N, "preview_rows": M}
        """
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
                    headers=self._cors_headers(request),
                )

        session_id = request.rel_url.query.get("session_id", "").strip()
        account_id = request.rel_url.query.get("account_id", "").strip()
        user_id = request.rel_url.query.get("user_id", "").strip()
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
                headers=self._cors_headers(request),
            )

        session_key = f"webui:{session_id}"
        payload_str = await self._get_discovery_payload(session_key, account_id, user_id, which)

        if payload_str is None:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "No discovery results found for this session"}),
                headers=self._cors_headers(request),
            )

        all_rows = _rows_from_payload(payload_str)
        if not all_rows:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "Discovery result contains no rows"}),
                headers=self._cors_headers(request),
            )

        total = len(all_rows)
        preview = [_flatten_preview_record(r) for r in all_rows[:max_rows]]
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
            headers=self._cors_headers(request),
        )

    async def _handle_list_sessions(self, request: Any) -> Any:
        """List all WebUI sessions with title preview, ordered by most-recent.

        Query params:
          account_id — required; scopes to this tenant's sessions
        Response: JSON array of {session_id, title, updated_at}
        """
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
                    headers=self._cors_headers(request),
                )

        account_id = request.rel_url.query.get("account_id", "").strip()
        user_id = request.rel_url.query.get("user_id", "").strip()

        _db = self._effective_db_manager
        if _db is None:
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps([]),
                headers=self._cors_headers(request),
            )

        try:
            async with _db._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        s.session_id,
                        s.updated_at,
                        (
                            SELECT (m.content->>'content')
                            FROM session_messages m
                            WHERE m.session_id = s.session_id
                              AND m.role = 'user'
                            ORDER BY m.id ASC
                            LIMIT 1
                        ) AS title
                    FROM agent_sessions s
                    WHERE s.session_id LIKE 'webui:%'
                      AND ($1::text = '' OR s.account_id = $1)
                      AND ($2::text = '' OR s.user_id = $2)
                    ORDER BY s.updated_at DESC
                    LIMIT 50
                    """,
                    account_id,
                    user_id,
                )
        except Exception as exc:
            logger.error("list_sessions DB error: {}", exc)
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": "Database error"}),
                headers=self._cors_headers(request),
            )

        sessions = []
        for row in rows:
            full_key = row["session_id"]  # "webui:<uuid>"
            sid = full_key[len("webui:"):]
            title = (row["title"] or "New conversation")[:100]
            updated_at = row["updated_at"]
            sessions.append({
                "session_id": sid,
                "title": title,
                "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
            })

        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(sessions, ensure_ascii=False),
            headers=self._cors_headers(request),
        )

    async def _handle_session_messages(self, request: Any) -> Any:
        """Return the message history for a WebUI session (for UI rendering on session switch).

        Path param:
          session_id — the bare UUID (without 'webui:' prefix)
        Query params:
          account_id — optional; used for future tenant filtering
        Response: JSON array of {role, content, timestamp}
        """
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
                    headers=self._cors_headers(request),
                )

        raw_session_id = request.match_info["session_id"].strip()
        account_id = request.rel_url.query.get("account_id", "").strip()
        user_id = request.rel_url.query.get("user_id", "").strip()

        if not raw_session_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "session_id required"}),
                headers=self._cors_headers(request),
            )

        _db = self._effective_db_manager
        if _db is None:
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps([]),
                headers=self._cors_headers(request),
            )

        session_key = f"webui:{raw_session_id}"
        try:
            rows = await _db.get_messages_for_session(
                session_key, limit=200, account_id=account_id, user_id=user_id
            )
        except Exception as exc:
            logger.error("session_messages DB error for {}: {}", raw_session_id, exc)
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": "Database error"}),
                headers=self._cors_headers(request),
            )

        messages = []
        for row in rows:
            role = row.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content_raw = row.get("content", {})
            if isinstance(content_raw, dict):
                content_str = content_raw.get("content", "")
                if isinstance(content_str, list):
                    content_str = " ".join(
                        p.get("text", "") for p in content_str if isinstance(p, dict)
                    )
            elif isinstance(content_raw, str):
                content_str = content_raw
            else:
                content_str = ""
            if not content_str:
                continue
            ts = row.get("created_at", "")
            messages.append({
                "role": role,
                "content": content_str,
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            })

        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(messages, ensure_ascii=False),
            headers=self._cors_headers(request),
        )

    async def _handle_delete_session(self, request: Any) -> Any:
        """Delete a session and all its messages.

        Path param:
          session_id — the bare UUID (without 'webui:' prefix)
        Query params:
          account_id — optional; scopes the delete to this tenant for safety
        Response: {"deleted": true} or 404 if not found
        """
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
                    headers=self._cors_headers(request),
                )

        raw_session_id = request.match_info["session_id"].strip()
        account_id = request.rel_url.query.get("account_id", "").strip()

        if not raw_session_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "session_id required"}),
                headers=self._cors_headers(request),
            )

        session_key = f"webui:{raw_session_id}"

        _db = self._effective_db_manager
        if _db is None:
            return web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": "Database not available"}),
                headers=self._cors_headers(request),
            )

        try:
            async with _db._pool.acquire() as conn:
                result = await conn.execute(
                    """
                    DELETE FROM agent_sessions
                    WHERE session_id = $1
                      AND ($2::text = '' OR account_id = $2)
                    """,
                    session_key,
                    account_id,
                )
            deleted_count = int(result.split()[-1]) if result else 0
        except Exception as exc:
            logger.error("delete_session DB error for {}: {}", raw_session_id, exc)
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": "Database error"}),
                headers=self._cors_headers(request),
            )

        if deleted_count == 0:
            return web.Response(
                status=404,
                content_type="application/json",
                text=json.dumps({"error": "Session not found"}),
                headers=self._cors_headers(request),
            )

        # Clean up in-memory state for this session
        self._session_locks.pop(raw_session_id, None)
        self._queues.pop(raw_session_id, None)

        logger.info("Deleted session {} (account={})", raw_session_id, account_id or "*")
        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"deleted": True, "session_id": raw_session_id}),
            headers=self._cors_headers(request),
        )

    async def _get_mcp_manager(self) -> Any:
        """Get or create MCP prompt manager."""
        logger.debug("MCP config: {}", self._mcp_config)
        if self._mcp_manager is None and self._mcp_config:
            from nanobot.agent.tools.mcp import MCPPromptManager

            logger.info("Creating MCP prompt manager with config: {}", list(self._mcp_config.keys()))
            self._mcp_manager = MCPPromptManager(self._mcp_config, tool_timeout=300)
        elif not self._mcp_config:
            logger.warning("MCP config is empty or not provided")
        return self._mcp_manager

    async def _handle_get_dna(self, request: Any) -> Any:
        """Return the company DNA for an account.
        GET /api/accounts/{account_id}/dna
        """
        from aiohttp import web

        account_id = request.match_info["account_id"]
        headers = self._cors_headers(request)
        db = self._effective_db_manager
        if not db:
            return web.Response(status=503, text="DB unavailable", headers=headers)
        dna = await db.get_account_dna(account_id)
        if dna is None:
            return web.Response(status=404, text="No DNA configured for this account", headers=headers)
        return web.Response(
            status=200,
            text=__import__("json").dumps(dna, ensure_ascii=False),
            content_type="application/json",
            headers=headers,
        )

    async def _handle_put_dna(self, request: Any) -> Any:
        """Replace the company DNA for an account.
        PUT /api/accounts/{account_id}/dna
        Body: JSON object (the full company_dna dict)
        """
        from aiohttp import web
        import json as _json

        account_id = request.match_info["account_id"]
        headers = self._cors_headers(request)
        db = self._effective_db_manager
        if not db:
            return web.Response(status=503, text="DB unavailable", headers=headers)
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="Invalid JSON body", headers=headers)
        if not isinstance(body, dict):
            return web.Response(status=400, text="Body must be a JSON object", headers=headers)
        await db.upsert_account_dna(account_id, body)
        # Invalidate the agent loop's in-process DNA cache for this account
        if hasattr(self, "_agent_loop") and self._agent_loop:
            self._agent_loop._account_dna_cache.pop(account_id, None)
        return web.Response(
            status=200,
            text=_json.dumps({"updated": True, "account_id": account_id}),
            content_type="application/json",
            headers=headers,
        )

    async def _handle_list_mcp_prompts(self, request: Any) -> Any:
        """List available MCP prompts for slash command autocomplete."""
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
                    headers=self._cors_headers(request),
                )

        account_id = request.query.get("account_id", "")
        if not account_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "account_id is required"}),
                headers=self._cors_headers(request),
            )

        mcp_manager = await self._get_mcp_manager()
        if mcp_manager is None:
            return web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": "MCP not configured"}),
                headers=self._cors_headers(request),
            )

        try:
            prompts = await mcp_manager.list_prompts(account_id)
            logger.info("Loaded {} MCP prompts for account {}", len(prompts), account_id)
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps({"prompts": prompts}, ensure_ascii=False),
                headers=self._cors_headers(request),
            )
        except Exception as e:
            logger.exception("Failed to list MCP prompts: {}", e)
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": str(e)}),
                headers=self._cors_headers(request),
            )

    async def _handle_parse_command(self, request: Any) -> Any:
        """Parse a raw slash-command string into a structured {prompt, args} object.

        POST /api/parse-command
        Body: {"text": "/segments Mani MSP", "account_id": "<id>"}
        Returns: {"prompt": "segments", "args": {"name": "Mani MSP"}}

        The frontend can call this to convert a user-typed slash command into the
        structured form expected by POST /api/mcp/prompt.
        """
        from aiohttp import web

        try:
            body = await request.json()
        except Exception:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "Invalid JSON body"}),
                headers=self._cors_headers(request),
            )

        text       = (body.get("text") or "").strip()
        account_id = (body.get("account_id") or "").strip()

        if not text:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "text is required"}),
                headers=self._cors_headers(request),
            )
        if not text.startswith("/"):
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "text must start with /"}),
                headers=self._cors_headers(request),
            )

        # Fetch prompt schemas so the parser can map positional args correctly.
        prompt_schemas: list[dict] = []
        if account_id:
            try:
                mcp_manager = await self._get_mcp_manager()
                if mcp_manager is not None:
                    prompt_schemas = await mcp_manager.list_prompts(account_id)
            except Exception:
                pass  # Parse without schema — positional mapping degrades gracefully

        try:
            prompt_name, args = self._parse_slash_command(text, prompt_schemas)
        except ValueError as exc:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": str(exc)}),
                headers=self._cors_headers(request),
            )

        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps({"prompt": prompt_name, "args": args}, ensure_ascii=False),
            headers=self._cors_headers(request),
        )

    # ── Slash-command text parser ──────────────────────────────────────────────

    # Per-prompt aliases: short key → canonical arg name.
    # Lets users type `linkedin=<url>` instead of `linkedin_url=<url>`.
    _ARG_ALIASES: dict[str, dict[str, str]] = {
        "enrich": {
            "linkedin":   "linkedin_url",
            "url":        "linkedin_url",
            "first":      "first_name",
            "last":       "last_name",
            "website":    "company_website",
            "domain":     "company_website",
            "company":    "company_website",
            "field":      "fields",
        },
        "segment":  {"id": "segment_id", "name": "segment_id"},
        "segments": {"query": "name"},
        "dedupe":   {"id": "segment_id", "name": "segment_id"},
    }

    @staticmethod
    def _parse_slash_command(
        text: str,
        prompt_schemas: list[dict],
    ) -> tuple[str, dict[str, str]]:
        """Parse a slash-command string into (prompt_name, args_dict).

        Syntax rules
        ------------
        Tokens are split with shlex (respects double-quoted strings).

        • key=value  → named arg   (key may be an alias; see _ARG_ALIASES)
        • bare token → positional  (mapped to the first unfilled arg in schema order)
        • Special: a bare token that starts with "http" or "linkedin.com" inside
          /enrich → mapped directly to linkedin_url.

        Examples
        --------
        /segments Mani MSP
            → ("segments", {"name": "Mani MSP"})

        /segments "Mani MSP"
            → ("segments", {"name": "Mani MSP"})

        /segment Mani MSP
            → ("segment", {"segment_id": "Mani MSP"})

        /enrich linkedin_url=https://linkedin.com/in/foo fields=email,phone
            → ("enrich", {"linkedin_url": "https://...", "fields": "email,phone"})

        /enrich linkedin=https://linkedin.com/in/foo fields=email,phone
            → ("enrich", {"linkedin_url": "https://...", "fields": "email,phone"})

        /enrich https://linkedin.com/in/foo
            → ("enrich", {"linkedin_url": "https://..."})

        /merge rs_abc rs_xyz
            → ("merge", {"result_set_id_a": "rs_abc", "result_set_id_b": "rs_xyz"})

        /merge rs_abc rs_xyz strategy=email_only
            → ("merge", {"result_set_id_a": "rs_abc", "result_set_id_b": "rs_xyz",
                          "strategy": "email_only"})
        """
        import shlex as _shlex

        text = text.strip()
        if not text.startswith("/"):
            raise ValueError("Command must start with /")

        try:
            tokens = _shlex.split(text)
        except ValueError:
            # Unmatched quotes — fall back to naive split
            tokens = text.split()

        if not tokens:
            raise ValueError("Empty command")

        command = tokens[0].lstrip("/").lower()
        remaining = tokens[1:]

        # Look up schema so we know arg order
        schema = next((p for p in prompt_schemas if p["name"] == command), None)
        schema_args: list[dict] = schema.get("arguments", []) if schema else []

        # Resolve alias table for this command
        aliases = WebUIChannel._ARG_ALIASES.get(command, {})

        named: dict[str, str] = {}
        positional: list[str] = []

        for token in remaining:
            eq = token.find("=")
            # key=value: eq must exist, key must not look like a URL path
            if eq > 0 and "/" not in token[:eq]:
                raw_key = token[:eq]
                value   = token[eq + 1:]
                canon   = aliases.get(raw_key, raw_key)
                named[canon] = value
            else:
                positional.append(token)

        # Special-case /enrich: bare URL → linkedin_url
        if command == "enrich" and positional:
            first = positional[0]
            if first.startswith("http") or first.startswith("linkedin.com"):
                named.setdefault("linkedin_url", first)
                positional = positional[1:]

        # Map remaining positional tokens to schema args (skip already-named ones)
        unfilled = [a["name"] for a in schema_args if a["name"] not in named]
        if positional and unfilled:
            if len(unfilled) == 1:
                # Single slot: join all positional tokens as one string
                named[unfilled[0]] = " ".join(positional)
            else:
                # Multiple slots: assign one token per slot
                for slot, token in zip(unfilled, positional):
                    named[slot] = token
                # If tokens remain after slots are filled, append to last slot
                if len(positional) > len(unfilled):
                    last_slot = unfilled[-1]
                    named[last_slot] = " ".join(
                        [named[last_slot]] + positional[len(unfilled):]
                    )

        return command, named

    # ── Prompt name → skill name ───────────────────────────────────────────────

    # Prompt name → skill name to inject as ephemeral context for the LLM turn.
    _PROMPT_SKILL_MAP: dict[str, str] = {
        "discovery":       "hp-discovery",
        "find":            "hp-discovery",   # natural-language fallback
        "segment":         "hp-segments",
        "segments":        "hp-segments",
        "enrich":          "hp-enrich",
        "merge":           "hp-dataops",
        "dedupe":          "hp-dataops",
        "push-to-segment": "hp-dataops",
    }

    async def _pre_analyze_discovery(
        self,
        *,
        args: dict,
        account_id: str,
        sse_response: Any,
    ) -> dict:
        """Run NanoBot-side intent analysis on a /discovery query.

        1. Rewrites "my ICP" / "our ICP" / "our personas" with actual titles from
           the account's Company DNA so the MCP never needs to know about DNA.
        2. Calls IntentAnalysisTool.execute() — streams translated reasoning as SSE
           progress events so the user sees activity while the model thinks.
        3. Maps strategy (1/2/3) → search_type ("direct"/"deep_research"/"hybrid").
        4. Returns a new args dict with 'query', 'search_type', and (for hybrid)
           'contact_query' set.  The original dict is never mutated.
        """
        raw_query = args["query"].strip()

        # ── 1. Rewrite ICP references using DNA ─────────────────────────────
        if self._agent_loop is not None and account_id:
            try:
                dna = await self._agent_loop._get_account_dna(account_id)
                if dna:
                    rewritten = _rewrite_icp_with_dna(raw_query, dna)
                    if rewritten != raw_query:
                        logger.info(
                            "discovery: ICP rewrite: {!r} → {!r}",
                            raw_query[:80], rewritten[:80],
                        )
                    raw_query = rewritten
            except Exception as exc:
                logger.warning("discovery: DNA rewrite failed: {}", exc)

        # ── 2. Get intent tool ───────────────────────────────────────────────
        intent_tool = (
            self._agent_loop.tools.get("analyze_enrichment_intent")
            if self._agent_loop is not None else None
        )
        if intent_tool is None:
            logger.debug("discovery: intent tool not found — using raw query")
            return {**args, "query": raw_query}

        # ── 3. SSE progress bridge ───────────────────────────────────────────
        async def _progress(msg: str) -> None:
            try:
                await self._write_sse(
                    sse_response, event="progress",
                    data={"type": "progress", "content": msg},
                )
            except Exception:
                pass

        thinking_translator = (
            self._agent_loop.config.agents.defaults.thinking_translator_model
            if self._agent_loop is not None else None
        )
        intent_tool.set_context(
            account_id=account_id,
            on_progress=_progress,
            thinking_translator_model=thinking_translator,
        )

        # ── 4. Run intent analysis ───────────────────────────────────────────
        try:
            intent_json = await intent_tool.execute(user_query=raw_query)
            intent = json.loads(intent_json)
        except Exception as exc:
            logger.warning("discovery: intent analysis failed: {} — using raw query", exc)
            return {**args, "query": raw_query}

        if "error" in intent:
            logger.warning("discovery: intent returned error: {} — using raw query", intent["error"])
            return {**args, "query": raw_query}

        # ── 5. Map strategy → search_type + extract formed queries ──────────
        strategy = intent.get("strategy", 1)
        mcp_call = intent.get("mcp_call") or {}

        if strategy == 3:  # Hybrid: deep research companies → DB contacts
            search_type = "hybrid"
            phase1 = mcp_call.get("phase_1") or {}
            phase2 = mcp_call.get("phase_2") or {}
            formed_query = (phase1.get("params") or {}).get("query") or raw_query
            contact_query = (phase2.get("params") or {}).get("query") or ""
        elif strategy == 2:  # Deep research only
            search_type = "deep_research"
            formed_query = (mcp_call.get("params") or {}).get("query") or raw_query
            contact_query = ""
        else:  # Strategy 1 — direct DB
            search_type = "direct"
            formed_query = (mcp_call.get("params") or {}).get("query") or raw_query
            contact_query = ""

        logger.info(
            "discovery pre-analysis: strategy={}, search_type={}, query={!r}",
            strategy, search_type, formed_query[:100],
        )

        new_args: dict = {**args, "query": formed_query, "search_type": search_type}
        if contact_query:
            new_args["contact_query"] = contact_query
        return new_args

    async def _handle_mcp_export(self, request: Any) -> Any:
        """Redirect browser to a pre-signed S3 CSV download URL.

        The MCP server uploads the CSV to S3 and returns a pre-signed URL.
        This endpoint fetches that URL and issues a 302 redirect so the browser
        downloads directly from S3 — no bytes are proxied through NanoBot.

        Query params:
          resultset_id  — MCP server resultset ID (e.g. rs_xxxxx)
          account_id    — tenant scope
          user_id       — optional
          session_id    — optional
        """
        from aiohttp import web

        q = request.rel_url.query
        resultset_id = q.get("resultset_id", "").strip()
        account_id   = q.get("account_id",   "").strip()
        user_id      = q.get("user_id",      "").strip()
        session_id   = q.get("session_id",   "").strip()

        if not resultset_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "resultset_id is required"}),
                headers=self._cors_headers(request),
            )

        mcp_manager = await self._get_mcp_manager()
        if mcp_manager is None:
            return web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": "MCP not configured"}),
                headers=self._cors_headers(request),
            )

        try:
            download_url = await mcp_manager.download_export(
                account_id=account_id,
                resultset_id=resultset_id,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as exc:
            logger.error("MCP export failed for resultset_id={}: {}", resultset_id, exc)
            return web.Response(
                status=502,
                content_type="application/json",
                text=json.dumps({"error": f"Export failed: {exc}"}),
                headers=self._cors_headers(request),
            )

        # 302 redirect — browser follows to S3 and downloads the file directly
        raise web.HTTPFound(location=download_url)

    async def _handle_mcp_prompt(self, request: Any) -> Any:
        """Execute an MCP prompt (slash command) with synthetic context injection.

        Flow:
          1. Call MCP server directly (fast path — no LLM on data fetch).
          2. Persist the resultset to Postgres.
          3. Inject a synthetic assistant tool-call + tool-result into the session.
          4. Run the LLM (with the matching skill loaded) so it can present the
             data properly and hold the resultset_id in context for follow-ups.
          5. Stream the LLM response back via SSE.
        """
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
                    headers=self._cors_headers(request),
                )

        try:
            body = await request.json()
        except Exception:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "Invalid JSON body"}),
                headers=self._cors_headers(request),
            )

        prompt_name = body.get("prompt", "").strip()
        args = body.get("args", {})
        session_id = body.get("session_id", "")
        account_id = body.get("account_id", "")
        user_id = body.get("user_id", "")

        # Allow raw slash-command text as an alternative to structured prompt+args.
        # e.g. {"command": "/segments Mani MSP", "account_id": "...", "session_id": "..."}
        raw_command = body.get("command", "").strip()
        if raw_command and not prompt_name:
            prompt_schemas: list[dict] = []
            try:
                _mgr = await self._get_mcp_manager()
                if _mgr is not None:
                    prompt_schemas = await _mgr.list_prompts(account_id)
            except Exception:
                pass
            try:
                prompt_name, args = self._parse_slash_command(raw_command, prompt_schemas)
            except ValueError as exc:
                return web.Response(
                    status=400,
                    content_type="application/json",
                    text=json.dumps({"error": str(exc)}),
                    headers=self._cors_headers(request),
                )

        logger.info("MCP prompt: prompt={}, account_id={}", prompt_name, account_id)

        if not prompt_name:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "prompt is required"}),
                headers=self._cors_headers(request),
            )
        if not account_id:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "account_id is required"}),
                headers=self._cors_headers(request),
            )

        mcp_manager = await self._get_mcp_manager()
        if mcp_manager is None:
            return web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": "MCP not configured"}),
                headers=self._cors_headers(request),
            )

        # Open SSE stream immediately so the client sees activity.
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **self._cors_headers(request),
            },
        )
        await response.prepare(request)

        try:
            await self._write_sse(
                response, event="progress",
                data={"type": "progress", "content": f"Running /{prompt_name}…"},
            )

            # ── 0. Discovery intent pre-analysis (NanoBot-side routing) ────
            # For /discovery: run intent analysis locally, rewrite ICP refs
            # from DNA, then pass search_type + formed query to the MCP so it
            # can route directly to the right API endpoint.
            if prompt_name == "discovery" and args.get("query", "").strip():
                args = await self._pre_analyze_discovery(
                    args=args,
                    account_id=account_id,
                    sse_response=response,
                )

            # ── 1. Direct MCP call (fast, no LLM) ─────────────────────────
            prompt_result = await mcp_manager.invoke_prompt(
                account_id, prompt_name, args,
                user_id=user_id, session_id=session_id,
            )
            code = prompt_result.get("code")
            logger.debug("MCP prompt code (first 200): {}", code[:200] if code else "NONE")

            if not code:
                await self._write_sse(
                    response, event="error",
                    data={"type": "error", "content": "No code returned from prompt"},
                )
                return response

            await self._write_sse(
                response, event="progress",
                data={"type": "progress", "content": "Fetching data…"},
            )

            result, _ = await mcp_manager.execute_code(
                account_id, code,
                user_id=user_id, session_id=session_id,
            )

            if isinstance(result, dict) and "error" in result:
                await self._write_sse(
                    response, event="error",
                    data={"type": "error", "content": result["error"]},
                )
                return response

            result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

            # ── 2. Parse result JSON (needed for both persistence and resultset_id) ──
            _parsed: dict | None = None
            try:
                _parsed_raw = json.loads(result_str)
                if isinstance(_parsed_raw, dict):
                    _parsed = _parsed_raw
            except Exception:
                pass

            # Extract MCP-server-assigned resultset_id if present.
            mcp_resultset_id: str | None = None
            if _parsed is not None:
                mcp_resultset_id = (
                    _parsed.get("resultset_id")
                    or _parsed.get("result_id")
                    or None
                )

            # ── 3. Persist resultset to Agent Postgres ─────────────────────
            resultset_ref: str | None = None
            if session_id and account_id:
                db = self._effective_db_manager
                if db is None:
                    logger.warning(
                        "MCP /{}: _effective_db_manager is None — resultset will not be stored "
                        "(agent_loop={}, agent_loop._db_manager={})",
                        prompt_name,
                        self._agent_loop is not None,
                        self._agent_loop._db_manager is not None if self._agent_loop else "N/A",
                    )
                else:
                    from nanobot.agent.history_prep import classify_json_result, make_discovery_label
                    _session_key = f"webui:{session_id}"
                    _tool_name = f"mcp_{prompt_name}"

                    # Determine rows + row_count from response.
                    # Priority: (a) explicit preview+total keys, (b) any list-of-dicts value,
                    # (c) classify_json_result fallback.
                    _payload: str | None = None
                    _row_count: int = 0
                    _shape: str = "object"

                    if _parsed is not None and "preview" in _parsed and "total" in _parsed:
                        # MCP structured shape: {resultset_id, preview, total, schema_summary}
                        preview_rows = _parsed.get("preview") or []
                        _row_count = int(_parsed.get("total") or len(preview_rows))
                        _shape = "contact_company"
                        _payload = json.dumps(preview_rows, ensure_ascii=False)
                    elif _parsed is not None:
                        # Scan all dict values for a list-of-dicts (handles any envelope key).
                        _best_rows: list = []
                        _best_total = int(_parsed.get("total") or _parsed.get("count") or 0)
                        for _v in _parsed.values():
                            if isinstance(_v, list) and _v and isinstance(_v[0], dict):
                                if len(_v) > len(_best_rows):
                                    _best_rows = _v
                        if _best_rows:
                            # Store first 20 rows as preview; use API total if provided.
                            _preview = _best_rows[:20]
                            _row_count = _best_total or len(_best_rows)
                            _shape = "wrapped"
                            _payload = json.dumps(_preview, ensure_ascii=False)
                        else:
                            # No list found — store the full object if it has content.
                            classification = classify_json_result(_tool_name, result_str)
                            logger.debug("MCP /{}: classify_json_result={}", prompt_name, classification)
                            if classification is not None:
                                _shape, _row_count = classification
                                _payload = result_str
                    else:
                        # result_str is a raw array or non-dict — fall back to classifier.
                        classification = classify_json_result(_tool_name, result_str)
                        logger.debug("MCP /{}: classify_json_result={}", prompt_name, classification)
                        if classification is not None:
                            _shape, _row_count = classification
                            _payload = result_str

                    if _payload is not None:
                        _label = make_discovery_label(_tool_name, _shape, _row_count, _payload)
                        try:
                            # Ensure the session row exists before writing session_messages
                            # (slash commands run before the agent creates the session lazily).
                            await db.upsert_session(
                                session_id=_session_key,
                                account_id=account_id,
                                user_id=user_id or "default_user",
                                channel="webui",
                            )
                            resultset_ref = await db.insert_discovery_result(
                                session_id=_session_key,
                                account_id=account_id,
                                user_id=user_id or "default_user",
                                tool_name=_tool_name,
                                payload=_payload,
                                label=_label,
                            )
                            logger.info(
                                "MCP /{}: stored resultset ref={}, shape={}, rows={}",
                                prompt_name, resultset_ref, _shape, _row_count,
                            )
                        except Exception as _e:
                            logger.warning("MCP /{}: failed to store resultset: {}", prompt_name, _e)
                    else:
                        logger.debug(
                            "MCP /{}: no storable rows detected in response (result_str[:200]={})",
                            prompt_name, result_str[:200],
                        )

            active_rs_id = mcp_resultset_id or resultset_ref or None
            logger.info(
                "MCP /{} complete: mcp_resultset_id={}, agent_resultset_ref={}, active_rs={}",
                prompt_name, mcp_resultset_id, resultset_ref, active_rs_id,
            )

            # ── 4. Build synthetic turns ───────────────────────────────────
            # Inject as if the LLM called the MCP tool and received the result.
            synth_call_id = f"synth_{uuid.uuid4().hex[:12]}"
            tool_name = f"mcp_{prompt_name}"
            synthetic_assistant: dict = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": synth_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }],
            }
            synthetic_tool_result: dict = {
                "role": "tool",
                "tool_call_id": synth_call_id,
                "name": tool_name,
                "content": result_str,
            }

            # ── 5. Route through agent loop for LLM presentation ───────────
            if self._agent_loop is not None and session_id:
                chat_id = session_id
                if session_id not in self._session_locks:
                    self._session_locks[session_id] = asyncio.Lock()
                lock = self._session_locks[session_id]

                queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
                self._queues[chat_id] = queue

                try:
                    user_query = args.get("query", args.get("input", ""))
                    user_text = (
                        f"/{prompt_name} {user_query}".strip()
                        if user_query else f"/{prompt_name}"
                    )
                    metadata: dict = {
                        "session_id": session_id,
                        "account_id": account_id,
                        "user_id": user_id,
                        "_synthetic_turns": [synthetic_assistant, synthetic_tool_result],
                        "_ephemeral_skill": self._PROMPT_SKILL_MAP.get(prompt_name),
                        "_synthetic_resultset_ref": resultset_ref or "",
                        "_synthetic_resultset_id": active_rs_id or "",
                        "_synthetic_label": _label if _payload is not None else "",
                        "_synthetic_row_count": _row_count if _payload is not None else 0,
                        # Pass raw MCP result so loop.py can stitch it with the
                        # agent's meta-only response into a single structured payload.
                        "_mcp_execute_latest": result_str,
                    }

                    async with lock:
                        await self._handle_message(
                            sender_id="user",
                            chat_id=chat_id,
                            content=user_text,
                            metadata=metadata,
                        )

                        # Drain queue — identical pattern to _handle_chat.
                        elapsed = 0.0
                        while elapsed < self._RESPONSE_TIMEOUT:
                            try:
                                out_msg = await asyncio.wait_for(
                                    queue.get(), timeout=self._KEEPALIVE_INTERVAL
                                )
                            except asyncio.TimeoutError:
                                await self._write_sse(response, comment="keepalive")
                                elapsed += self._KEEPALIVE_INTERVAL
                                continue

                            is_progress = out_msg.metadata.get("_progress", False)
                            is_streaming = out_msg.metadata.get("_streaming", False)
                            if is_progress:
                                if is_streaming:
                                    await self._write_sse(
                                        response, event="token",
                                        data={"type": "token", "content": out_msg.content},
                                    )
                                else:
                                    await self._write_sse(
                                        response, event="progress",
                                        data={"type": "progress", "content": out_msg.content},
                                    )
                            else:
                                _mcp_final: dict = {"type": "final", "content": out_msg.content}
                                if _rs_id := (out_msg.metadata or {}).get("active_resultset_id"):
                                    _mcp_final["active_resultset_id"] = _rs_id
                                if _sr := (out_msg.metadata or {}).get("structured_response"):
                                    _mcp_final["response"] = _sr
                                await self._write_sse(response, event="final", data=_mcp_final)
                                break
                        else:
                            await self._write_sse(
                                response, event="error",
                                data={"type": "error", "content": "Response timed out"},
                            )
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                    logger.debug("WebUI: client disconnected during mcp_prompt for {}", session_id)
                except Exception as _llm_err:
                    logger.error("MCP prompt LLM presentation failed: {}", _llm_err)
                    # Fall back: show the raw MCP result so the user at least sees data.
                    try:
                        await self._write_sse(
                            response, event="final",
                            data={"type": "final", "content": result_str},
                        )
                    except Exception:
                        pass
                finally:
                    self._queues.pop(chat_id, None)

            else:
                # No agent loop wired yet — fall back to direct result stream.
                await self._write_sse(
                    response, event="final",
                    data={"type": "final", "content": result_str},
                )

        except asyncio.TimeoutError:
            await self._write_sse(
                response, event="error",
                data={"type": "error", "content": "MCP execute timed out after 300s"},
            )
        except Exception as e:
            logger.error("MCP prompt execution failed: {}", e)
            try:
                await self._write_sse(
                    response, event="error",
                    data={"type": "error", "content": str(e)},
                )
            except Exception:
                pass

        return response

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
                    headers=self._cors_headers(request),
                )

        # --- Parse body -------------------------------------------------
        try:
            body = await request.json()
        except Exception:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "Invalid JSON body"}),
                headers=self._cors_headers(request),
            )

        content: str = body.get("content", "").strip()
        if not content:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({"error": "'content' field is required"}),
                headers=self._cors_headers(request),
            )

        # Accept both camelCase (sessionId, accountId, userId) and snake_case variants.
        session_id: str = (
            body.get("session_id") or body.get("sessionId") or str(uuid.uuid4())
        )
        account_id: str = (body.get("account_id") or body.get("accountId") or "").strip()
        user_id: str = (body.get("user_id") or body.get("userId") or "").strip()

        # Reject early if required identity fields are absent — avoids a wasted LLM call.
        missing = [f for f, v in [("account_id", account_id), ("user_id", user_id)] if not v]
        if missing:
            return web.Response(
                status=400,
                content_type="application/json",
                text=json.dumps({
                    "error": f"Missing required fields: {', '.join(missing)}",
                    "missing_fields": missing,
                }),
                headers=self._cors_headers(request),
            )

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
                **self._cors_headers(request),
            },
        )
        await response.prepare(request)

        async with lock:
            # Register queue for this chat_id
            queue: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=1000)
            self._queues[chat_id] = queue

            try:
                # Publish user message to the agent bus
                metadata: dict = {
                    "session_id": session_id,
                    "account_id": account_id,
                    "user_id": user_id,
                }
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
                        _final_data: dict = {"type": "final", "content": msg.content}
                        if _rs_id := (msg.metadata or {}).get("active_resultset_id"):
                            _final_data["active_resultset_id"] = _rs_id
                        if _sr := (msg.metadata or {}).get("structured_response"):
                            _final_data["response"] = _sr
                        await self._write_sse(response, event="final", data=_final_data)
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
