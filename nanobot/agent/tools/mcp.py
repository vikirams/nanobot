"""MCP client: connects to MCP servers and wraps their tools as native nanobot tools."""

import asyncio
import copy
import json
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from mcp import ClientSession


class MCPPromptManager:
    """Manages MCP prompts and sessions for slash command invocation.
    
    Provides a session pool keyed by account_id for efficient reuse.
    """

    def __init__(self, mcp_config: dict, tool_timeout: int = 300):
        self._mcp_config = mcp_config
        self._tool_timeout = tool_timeout
        self._sessions: dict[str, "ClientSession"] = {}
        self._sessions_lock = asyncio.Lock()
        self._exit_stack: AsyncExitStack | None = None
        self._http_clients: dict[str, httpx.AsyncClient] = {}

    async def _ensure_connected(self) -> None:
        """Ensure MCP connection is established."""
        if self._exit_stack is None:
            self._exit_stack = AsyncExitStack()
            await self._exit_stack.__aenter__()

    async def _create_session(self, account_id: str) -> "ClientSession":
        """Create a new MCP session for an account."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        await self._ensure_connected()

        for name, cfg in self._mcp_config.items():
            try:
                transport_type = cfg.type
                if not transport_type:
                    if cfg.command:
                        transport_type = "stdio"
                    elif cfg.url:
                        transport_type = (
                            "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                        )
                    else:
                        continue

                if transport_type == "stdio":
                    params = StdioServerParameters(
                        command=cfg.command, args=cfg.args, env=cfg.env or None
                    )
                    read, write = await self._exit_stack.enter_async_context(
                        stdio_client(params)
                    )
                elif transport_type == "sse":
                    def httpx_client_factory(
                        headers: dict[str, str] | None = None,
                        timeout: httpx.Timeout | None = None,
                        auth: httpx.Auth | None = None,
                    ) -> httpx.AsyncClient:
                        merged_headers = {**(cfg.headers or {}), **(headers or {})}
                        return httpx.AsyncClient(
                            headers=merged_headers or None,
                            follow_redirects=True,
                            timeout=timeout,
                            auth=auth,
                        )

                    read, write = await self._exit_stack.enter_async_context(
                        sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                    )
                elif transport_type == "streamableHttp":
                    http_client = httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                    self._http_clients[account_id] = http_client
                    await self._exit_stack.enter_async_context(http_client)
                    read, write, _ = await self._exit_stack.enter_async_context(
                        streamable_http_client(cfg.url, http_client=http_client)
                    )
                else:
                    logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                    continue

                session = await self._exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()

                logger.info("MCP session created for account '{}' with server '{}'", account_id, name)
                return session

            except Exception as e:
                logger.error("MCP server '{}': failed to create session: {}", name, e)
                continue

        raise RuntimeError("No MCP servers available or all connections failed")

    async def get_session(self, account_id: str) -> "ClientSession":
        """Get or create an MCP session for the given account."""
        async with self._sessions_lock:
            if account_id not in self._sessions:
                self._sessions[account_id] = await self._create_session(account_id)
            else:
                session = self._sessions[account_id]
                # Use ping instead of initialize — ping is a lightweight no-op that
                # doesn't re-do the full handshake. initialize() was designed to be
                # called once at connection time, not as a repeated health check.
                try:
                    await asyncio.wait_for(session.send_ping(), timeout=5)
                except Exception as e:
                    logger.warning("Session ping failed for account {}, recreating: {}", account_id, e)
                    self._sessions[account_id] = await self._create_session(account_id)
            return self._sessions[account_id]

    async def _ensure_session_valid(self, account_id: str) -> "ClientSession":
        """Ensure session is valid, recreate if needed."""
        try:
            session = await self.get_session(account_id)
            await session.list_tools()
            return session
        except Exception as e:
            logger.warning("Session test failed, recreating: {}", e)
            async with self._sessions_lock:
                if account_id in self._sessions:
                    del self._sessions[account_id]
            return await self._create_session(account_id)

    async def list_prompts(self, account_id: str) -> list[dict]:
        """Discover available prompts from MCP server."""
        session = await self.get_session(account_id)
        result = await session.list_prompts()
        return [
            {
                "name": p.name,
                "description": p.description,
                "arguments": [
                    {
                        "name": a.name,
                        "description": a.description,
                        "required": a.required,
                    }
                    for a in (p.arguments or [])
                ],
            }
            for p in result.prompts
        ]

    async def invoke_prompt(
        self,
        account_id: str,
        prompt_name: str,
        arguments: dict,
        user_id: str = "",
        session_id: str = "",
    ) -> dict:
        """Invoke a prompt and return the generated code."""
        # Merge identity fields so the MCP server can scope its response correctly.
        full_arguments = {
            **arguments,
            "_account_id": account_id,
            "_user_id": user_id,
            "_session_id": session_id,
        }
        logger.info(
            "invoke_prompt: prompt={}, account_id={}, user_id={}, session_id={}, args={}",
            prompt_name, account_id, user_id, session_id,
            {k: v for k, v in arguments.items() if not k.startswith("_")},
        )
        session = await self._ensure_session_valid(account_id)
        result = await session.get_prompt(prompt_name, arguments=full_arguments)

        import re

        code = None
        if result.messages:
            for msg in result.messages:
                if hasattr(msg.content, "text"):
                    text = msg.content.text
                    # The prompt may return markdown with the JS inside a fenced code block
                    match = re.search(r"```(?:javascript|js)?\n(.*?)```", text, re.DOTALL)
                    if match:
                        code = match.group(1).strip()
                    else:
                        code = text
                    break

        logger.info("invoke_prompt: generated code (first 300 chars): {}", code[:300] if code else "NONE")
        return {"code": code}

    async def execute_code(
        self,
        account_id: str,
        code: str,
        user_id: str = "",
        session_id: str = "",
    ) -> tuple[Any, AsyncIterator[str] | None]:
        """Execute code and return result with optional progress iterator."""
        session = await self._ensure_session_valid(account_id)

        # Always send identity context so the MCP server can scope storage + auth.
        execute_args: dict[str, Any] = {
            "code": code,
            "_account_id": account_id,
            "_user_id": user_id,
            "_session_id": session_id,
        }

        try:
            result = await asyncio.wait_for(
                session.call_tool("execute", execute_args),
                timeout=self._tool_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("MCP execute timed out after {}s", self._tool_timeout)
            raise TimeoutError(f"MCP execute timed out after {self._tool_timeout}s")

        from mcp import types

        parts = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(str(block))
        text = "\n".join(parts) or "(no output)"

        if getattr(result, "isError", False):
            return ({"error": text}, None)

        return (text, None)

    async def download_export(
        self,
        account_id: str,
        resultset_id: str,
        user_id: str = "",
        session_id: str = "",
        filename: str = "",
    ) -> str:
        """Return a pre-signed S3 download URL for a stored MCP resultset.

        Calls hp.exportCsv() via the MCP execute tool. The MCP server uploads
        the CSV to S3 and returns a pre-signed URL valid for 15 minutes.
        The caller (webui) redirects the browser directly to that URL — no
        proxying of bytes through NanoBot.
        """
        code = (
            f"async ({{ hp }}) => {{\n"
            f"  const r = await hp.exportCsv({json.dumps(resultset_id)});\n"
            f"  return r;\n"
            f"}}"
        )
        result, _ = await self.execute_code(
            account_id, code, user_id=user_id, session_id=session_id
        )
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except Exception:
            parsed = {}

        if isinstance(parsed, dict) and parsed.get("error"):
            raise RuntimeError(f"Export failed: {parsed['error']}")

        download_url = (parsed.get("download_url") or "") if isinstance(parsed, dict) else ""
        if not download_url:
            raise RuntimeError(
                f"MCP export returned no download_url for resultset_id={resultset_id!r}. "
                f"Ensure S3_BUCKET is configured on the MCP server. Raw result: {parsed!r}"
            )

        logger.info(
            "MCP export ready: resultset_id={} row_count={} url_preview={}…",
            resultset_id,
            parsed.get("row_count", "?"),
            download_url[:60],
        )
        return download_url

    async def close(self) -> None:
        """Close all MCP sessions."""
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._sessions.clear()
            self._http_clients.clear()


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as a nanobot Tool."""

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._session = session
        self._original_name = tool_def.name
        self._name = f"mcp_{server_name}_{tool_def.name}"
        self._description = tool_def.description or tool_def.name
        raw_schema = tool_def.inputSchema or {"type": "object", "properties": {}}
        # Strip private (_*) fields from the tool schema before showing it to the LLM.
        self._parameters = self._clean_schema(raw_schema)
        self._tool_timeout = tool_timeout

    @staticmethod
    def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
        """Strip private (_*) fields from the tool schema before showing it to the LLM.

        Fields like _account_id, _user_id, _session_id are injected automatically
        by the agent framework — exposing them to the LLM only causes confusion
        because the LLM doesn't know their values and may send {} instead of the
        real parameters.
        """
        schema = copy.deepcopy(schema)
        props: dict[str, Any] = schema.get("properties", {})
        private_keys = [k for k in props if k.startswith("_")]
        for k in private_keys:
            del props[k]
        required: list[str] = schema.get("required", [])
        if required:
            schema["required"] = [r for r in required if not r.startswith("_")]
        return schema

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        # Strip private injected fields before schema validation so they don't
        # confuse validate_params (they are not in the cleaned schema).
        public_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_")}

        # For search/execute tools the 'code' param is required at runtime even
        # when the MCP server marks it optional in its schema.  Catch this early
        # so the LLM gets a clear, actionable error instead of a cryptic Zod
        # exception from the server (e.g. "v3Schema.safeParseAsync is not a function").
        if (self._name.endswith("_search") or self._name.endswith("_execute")) and "code" not in public_kwargs:
            return (
                "(Invalid parameters: missing required 'code') "
                "For search/execute tools you MUST provide 'code' as a JavaScript async function string. "
                "Example: async (spec) => spec.filter(e => e.tags && e.tags.includes('segments'))"
                ".map(e => ({ method: e.method, path: e.path, params: e.params }))"
            )

        # Validate remaining required fields against schema
        errors = self.validate_params(public_kwargs)
        if errors:
            return f"(Invalid parameters: {'; '.join(errors)})"

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._original_name, arguments=kwargs),
                timeout=self._tool_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("MCP tool '{}' timed out after {}s", self._name, self._tool_timeout)
            return f"(MCP tool call timed out after {self._tool_timeout}s)"
        except asyncio.CancelledError:
            # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
            # Re-raise only if our task was externally cancelled (e.g. /stop).
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
            return "(MCP tool call was cancelled)"
        except Exception as exc:
            logger.exception(
                "MCP tool '{}' failed: {}: {}",
                self._name,
                type(exc).__name__,
                exc,
            )
            return f"(MCP tool call failed: {type(exc).__name__})"

        parts = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                parts.append(str(block))
        text = "\n".join(parts) or "(no output)"

        # MCP protocol signals tool-level errors via result.isError.
        # Wrap the text so the agent loop can detect it as an error and stop retrying.
        if getattr(result, "isError", False):
            return f'{{"error": {json.dumps(text)}}}'

        return text


async def connect_mcp_servers(
    mcp_servers: dict, registry: ToolRegistry, stack: AsyncExitStack
) -> None:
    """Connect to configured MCP servers and register their tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    for name, cfg in mcp_servers.items():
        try:
            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    # Convention: URLs ending with /sse use SSE transport; others use streamableHttp
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    continue

            if transport_type == "stdio":
                params = StdioServerParameters(
                    command=cfg.command, args=cfg.args, env=cfg.env or None
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":
                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {**(cfg.headers or {}), **(headers or {})}
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                # Always provide an explicit httpx client so MCP HTTP transport does not
                # inherit httpx's default 5s timeout and preempt the higher-level tool timeout.
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                continue

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            tools = await session.list_tools()
            for tool_def in tools.tools:
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)

            logger.info("MCP server '{}': connected, {} tools registered", name, len(tools.tools))
        except asyncio.CancelledError:
            # Re-raise so _connect_mcp can distinguish real task cancellation
            # from spurious CancelledError emitted by the MCP transport layer.
            raise
        except Exception as e:
            logger.error("MCP server '{}': failed to connect: {}", name, e)
