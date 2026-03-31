"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import uuid as _uuid
import weakref
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.history_prep import (
    is_tabular_tool_result,
    classify_json_result,
    get_result_kind,
    make_discovery_label,
    prepare_history_for_llm,
)
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.intent import IntentAnalysisTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.resultset import ListResultsetsTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider

from nanobot.hybrid_memory.stores import HybridMemoryStore, HybridSessionManager

from nanobot.config.schema import Config, ExecToolConfig
from nanobot import telemetry as _telemetry

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig
    from nanobot.cron.service import CronService
    from nanobot.db.manager import DBManager
    from nanobot.db.pgvector_manager import PGVectorManager

# Max result size (chars) before in-loop compaction is applied.
_IN_LOOP_RESULT_MAX: int = 8_000

# Tools whose results are metadata/summaries — never store as discovery results.
_EAGER_STORE_EXEMPT: frozenset[str] = frozenset({
    "analyze_enrichment_intent",
    "list_resultsets",
})


def _compact_tabular_result(
    result_str: str,
    max_preview_rows: int = 20,
    parsed: Any = None,
    result_kind: str = "contact_company",
) -> str:
    """Compact a large tabular JSON tool result for in-loop context."""
    _DISCOVERY_NOTE = (
        " Full {total} rows stored — result_kind=contact_company. "
        "Apply Data Receipt Protocol: output [Preview](#preview-last) sentinel, "
        "then show 📥 Download CSV / 📤 Push to Segment / 🔗 Push to Webhook buttons."
    )
    _MANAGEMENT_NOTE = (
        " Full {total} rows stored — result_kind=management_list. "
        "This is NOT a contact/company result. Render a compact inline markdown table only. "
        "Do NOT output [Preview](#preview-last). Do NOT show Download CSV button."
    )
    note_template = _DISCOVERY_NOTE if result_kind == "contact_company" else _MANAGEMENT_NOTE

    try:
        data = json.loads(result_str) if parsed is None else parsed

        rows = None
        envelope: dict | None = None

        if isinstance(data, list) and data and isinstance(data[0], dict):
            rows = data
        elif isinstance(data, dict):
            for key in ("results", "data", "items", "records"):
                candidate = data.get(key)
                if isinstance(candidate, list) and candidate and isinstance(candidate[0], dict):
                    rows = candidate
                    envelope = {k: v for k, v in data.items() if k != key}
                    break

        if rows is not None:
            total = len(rows)
            preview = rows[:max_preview_rows]
            base = envelope or {}
            base.update({
                "total": data.get("total", total) if isinstance(data, dict) else total,
                "preview_rows": len(preview),
                "note": f"Showing first {len(preview)} of {total} rows." + note_template.format(total=total),
                "data": preview,
            })
            return json.dumps(base)
    except Exception:
        pass

    kind_note = (
        "[contact_company result truncated]"
        if result_kind == "contact_company"
        else "[management_list result truncated — render inline, no CSV download]"
    )
    return result_str[:6_000] + f"\n{kind_note}"



class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 500

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        config: Config,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: "CronService | None" = None,
        restrict_to_workspace: bool = False,
        session_manager: Any = None,  # kept for API compatibility, ignored
        mcp_servers: dict | None = None,
        channels_config: "ChannelsConfig | None" = None,
    ):
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.config = config
        _telemetry.init(config.telemetry.posthog_api_key.get_secret_value(), config.telemetry.posthog_host)
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        # Postgres managers — lazily initialised in _ensure_db()
        self._db_pool = None
        self._db_manager: "DBManager | None" = None
        self._pgvec_manager: "PGVectorManager | None" = None
        self.sessions: HybridSessionManager | None = None
        self.memory_store: HybridMemoryStore | None = None
        self._ensure_db_lock = asyncio.Lock()
        self._ensure_db_done = False

        # Per-account company DNA cache — fetched once from DB, reused across turns.
        self._account_dna_cache: dict[str, dict] = {}

        # ChannelManager reference — set by commands.py after both objects are created.
        # Used in _ensure_db() to push the db_manager into WebUIChannel.
        self._channel_manager: Any = None

        self.context = ContextBuilder(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=reasoning_effort,
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        # Latest resultset_id per session key (for webui download routing).
        self._active_resultsets: dict[str, str] = {}
        # All resultsets seen this session: key → [{resultset_id, label, row_count, kind}]
        self._session_resultsets: dict[str, list[dict]] = {}
        self._consolidating: set[str] = set()
        self._consolidation_tasks: set[asyncio.Task] = set()
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._task_failures: dict[str, dict[str, Any]] = {}  # kept for compat, no longer used for retry
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
            path_append=self.exec_config.path_append,
        ))
        intent_model = self.config.agents.defaults.intent_model or self.model
        self.tools.register(IntentAnalysisTool(provider=self.provider, model=intent_model))

        # DB-dependent tools registered after DB init in _ensure_db()

        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _ensure_db(self) -> None:
        """Lazily initialise the PostgreSQL pool and all Postgres-specific managers.

        Called before the first message is processed.
        Thread-safe via an asyncio.Lock.
        """
        if self._ensure_db_done:
            return
        async with self._ensure_db_lock:
            if self._ensure_db_done:
                return
            from nanobot.db import connection as db_connection
            from nanobot.db.schema import init_schema
            from nanobot.db.manager import DBManager
            from nanobot.db.pgvector_manager import PGVectorManager

            pool = await db_connection.get_pool(
                self.config.database.url,
                min_size=self.config.database.pool_min_size,
                max_size=self.config.database.pool_max_size,
                command_timeout=self.config.database.pool_command_timeout,
                user=self.config.database.user,
                password=self.config.database.password,
            )
            await init_schema(pool)
            self._db_manager = DBManager(pool)
            self._pgvec_manager = PGVectorManager(
                pool, self.provider,
                embedding_dim=self.config.database.embedding_dim,
            )
            self.sessions = HybridSessionManager(
                self.workspace, db_manager=self._db_manager
            )
            self.memory_store = HybridMemoryStore(
                self.workspace,
                self._db_manager,
                self._pgvec_manager,
                provider=self.provider,
            )
            # Register DB-dependent tools now that db_manager is ready
            if not self.tools.get("list_resultsets"):
                self.tools.register(ListResultsetsTool(db_manager=self._db_manager))

            # Wire db_manager into WebUIChannel so its session/preview endpoints work.
            # The channel is created before _ensure_db() runs, so we push the reference now.
            if self._channel_manager is not None:
                webui = self._channel_manager.channels.get("webui")
                if webui is not None and hasattr(webui, "set_agent_loop"):
                    webui.set_agent_loop(self)

            self._ensure_db_done = True
            logger.info("Postgres path initialised successfully")

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                self._mcp_stack = None
                raise
            logger.warning("MCP connection interrupted (spurious CancelledError); will retry on next message")
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        message_id: Any = None,
        db_manager: Any = None,
        account_id: str = "",
        user_id: str = "",
        active_resultset_id: str = "",
    ) -> None:
        """Update context for all tools that need routing info."""
        session_key = f"{channel}:{chat_id}"
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

        # Wire session + DB context into list_resultsets
        if _tool := self.tools.get("list_resultsets"):
            if hasattr(_tool, "set_context"):
                _tool.set_context(
                    session_key,
                    db_manager=db_manager,
                    account_id=account_id,
                    user_id=user_id,
                    active_resultset_id=active_resultset_id,
                )

    def _push_session_resultset(
        self,
        session_key: str,
        resultset_id: str,
        label: str,
        row_count: int,
        kind: str = "contact_company",
    ) -> None:
        """Track a resultset in the per-session list (dedup by ID, keep last 10)."""
        datasets = self._session_resultsets.setdefault(session_key, [])
        # Remove existing entry for same ID (re-insert at end to move to latest position)
        datasets[:] = [d for d in datasets if d["resultset_id"] != resultset_id]
        datasets.append({"resultset_id": resultset_id, "label": label, "row_count": row_count, "kind": kind})
        self._session_resultsets[session_key] = datasets[-10:]

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    _DUPLICATE_WINDOW = 3

    @staticmethod
    def _extract_mcp_error(result_str: str, parsed: Any = None) -> tuple[str, str]:
        """Return (error_code, error_message) from an MCP error response.

        Handles two wire formats:
          • server.js direct:  {"error": "SOME_CODE", "message": "..."}
          • mcp.py isError:    {"error": "{\"code\":\"SOME_CODE\",\"message\":\"...\"}"}
        """
        try:
            outer = parsed if isinstance(parsed, dict) else json.loads(result_str)
            if isinstance(outer, dict):
                err_val = outer.get("error", "")
                msg_val = outer.get("message", "")
                if isinstance(err_val, str):
                    # Try nested JSON (mcp.py isError wrapping)
                    try:
                        inner = json.loads(err_val)
                        if isinstance(inner, dict):
                            code = inner.get("code") or inner.get("error") or "MCP_ERROR"
                            msg  = inner.get("message") or msg_val or err_val
                            return str(code), str(msg)
                    except Exception:
                        pass
                    # Direct code string (server.js / execute.js format)
                    return (err_val or "MCP_ERROR"), (msg_val or result_str[:300])
        except Exception:
            pass
        return "MCP_ERROR", result_str[:300]

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        session_key: str = "",
        account_id: str = "",
        user_id: str = "",
        db_mgr: "DBManager | None" = None,
        memory_store_ref: "HybridMemoryStore | None" = None,
    ) -> tuple[str | None, list[str], list[dict], dict[str, str], dict[str, str]]:
        """Run the agent iteration loop.

        Returns (final_content, tools_used, messages, raw_payloads, resultset_refs)
        where resultset_refs maps tool_call_id → resultset_ref.
        """
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        loop_start = time.monotonic()
        mcp_tools_called_this_turn: set[str] = set()
        # Full (uncompacted) payloads keyed by tool_call_id
        raw_payloads: dict[str, str] = {}
        # tool_call_id → resultset_ref
        resultset_refs: dict[str, str] = {}
        # Latest MCP-assigned resultset_id seen this turn (safety net for classification misses)
        _latest_mcp_resultset_id: str | None = None
        # Latest next_actions list from any MCP result this turn
        _latest_next_actions: list[str] | None = None
        # Clear per-task failures from previous loop (new user message = fresh start)
        self._task_failures.clear()
        # Track iterations with no successful tool calls (agent spinning)
        no_progress_iterations = 0
        max_no_progress = 3

        recent_tool_sigs: list[str] = []
        tool_defs = self.tools.get_definitions()

        while iteration < self.max_iterations:
            iteration += 1

            logger.info("[iter {}] LLM call ({} msgs)", iteration, len(messages))
            llm_t0 = time.monotonic()

            async def _stream_token(delta: str) -> None:
                if on_progress and delta:
                    await on_progress(delta, streaming=True)

            response = await self.provider.chat(
                messages=messages,
                tools=tool_defs,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
                on_token=_stream_token if on_progress else None,
            )
            llm_ms = int((time.monotonic() - llm_t0) * 1000)
            _telemetry.capture("agent.llm_call", {
                "session_key": session_key,
                "iteration": iteration,
                "model": self.model,
                "llm_ms": llm_ms,
                "message_count": len(messages),
                "has_tool_calls": response.has_tool_calls,
                "tool_names": [tc.name for tc in response.tool_calls] if response.has_tool_calls else [],
                "prompt_tokens": (response.usage or {}).get("prompt_tokens"),
                "completion_tokens": (response.usage or {}).get("completion_tokens"),
                "total_tokens": (response.usage or {}).get("total_tokens"),
            }, account_id=account_id)
            if response.has_tool_calls:
                tc_names = ", ".join(tc.name for tc in response.tool_calls)
                logger.info("[iter {}] LLM {}ms | tools: {}", iteration, llm_ms, tc_names)
            else:
                logger.info("[iter {}] LLM {}ms | final response", iteration, llm_ms)

            if response.has_tool_calls:
                if on_progress:
                    thought = self._strip_think(response.content)
                    if thought:
                        await on_progress(thought)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                # Duplicate detection
                duplicate_detected = False
                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    sig = f"{tool_call.name}:{hash(args_str)}"
                    if sig in recent_tool_sigs[-self._DUPLICATE_WINDOW:]:
                        logger.warning(
                            "[iter {}] Duplicate tool call detected: {}({}) — breaking loop",
                            iteration, tool_call.name, args_str[:120],
                        )
                        final_content = (
                            f"I detected a repeated call to `{tool_call.name}` and stopped "
                            "to avoid an infinite loop. The tool may be returning no results "
                            "or the task may need to be rephrased."
                        )
                        duplicate_detected = True
                        break
                    recent_tool_sigs.append(sig)
                if len(recent_tool_sigs) > self._DUPLICATE_WINDOW * 4:
                    recent_tool_sigs = recent_tool_sigs[-self._DUPLICATE_WINDOW * 2:]

                if duplicate_detected:
                    _skip_content = (
                        "Skipped: duplicate call to avoid infinite loop. "
                        "Present the discovery preview and export buttons from the previous result if available."
                    )
                    for tc in response.tool_calls:
                        messages = self.context.add_tool_result(
                            messages, tc.id, tc.name, _skip_content
                        )
                    break

                # Build exec_args for each tool call (strip 'page' param for MCP)
                prep: list[tuple[Any, dict]] = []
                for tool_call in response.tool_calls:
                    exec_args = tool_call.arguments
                    if (
                        tool_call.name.startswith("mcp_")
                        and isinstance(exec_args, dict)
                        and "page" in exec_args
                    ):
                        exec_args = {k: v for k, v in exec_args.items() if k != "page"}
                    prep.append((tool_call, exec_args))

                async def _run_one_tool(
                    tool_call: Any,
                    exec_args: dict,
                ) -> tuple[Any, str, int]:
                    _args = exec_args

                    if tool_call.name.startswith("mcp_") and isinstance(_args, dict):
                        _args = {
                            **_args,
                            "_account_id": account_id,
                            "_user_id": user_id,
                            "_session_id": session_key,
                        }
                    t0 = time.monotonic()
                    result = await self.tools.execute(tool_call.name, _args)
                    ms = int((time.monotonic() - t0) * 1000)
                    return (tool_call, str(result), ms)

                # Log outbound tool calls
                for tool_call, exec_args in prep:
                    args_str = json.dumps(exec_args, ensure_ascii=False)
                    if tool_call.name.startswith("mcp_"):
                        mcp_args_log = " | ".join(
                            f"query={str(exec_args[k])[:80]!r}" if k == "query"
                            else f"{k}={exec_args[k]}"
                            for k in ("type", "query", "limit") if k in exec_args
                        )
                        logger.info("[iter {}] → MCP {} | {}", iteration, tool_call.name, mcp_args_log)
                        # Debug: log full MCP request
                        logger.debug("[iter {}] MCP {} REQUEST: {}", iteration, tool_call.name, json.dumps(exec_args, ensure_ascii=False)[:500])
                    else:
                        logger.info("[iter {}] → {} | {}", iteration, tool_call.name, args_str[:150])
                    _telemetry.capture("agent.tool_called", {
                        "session_key": session_key,
                        "iteration": iteration,
                        "tool_name": tool_call.name,
                        "cache_hit": False,
                    }, account_id=account_id)

                # Execute all tools in parallel
                run_tasks = [_run_one_tool(p[0], p[1]) for p in prep]
                batch_results = await asyncio.gather(*run_tasks)

                # Process results
                for (tool_call, result_str, tool_ms) in batch_results:
                    _parsed: Any = None
                    try:
                        _parsed = json.loads(result_str)
                    except Exception:
                        pass

                    # Detect errors (JSON errors, timeouts starting with (, or Error: prefix)
                    is_error = (
                        result_str.startswith('{"error"') or
                        result_str.startswith("(") or
                        result_str.startswith("Error:")
                    ) if result_str else False

                    _telemetry.capture("agent.tool_result", {
                        "session_key": session_key,
                        "iteration": iteration,
                        "tool_name": tool_call.name,
                        "tool_ms": tool_ms,
                        "result_preview": result_str[:300] if result_str else "",
                        "is_error": is_error,
                        "cache_hit": False,
                    }, account_id=account_id)

                    # Intimate the channel about tool failures/timeouts immediately
                    if is_error and on_progress:
                        _clean_err = result_str
                        if result_str.startswith('{"error":'):
                            try:
                                _clean_err = json.loads(result_str).get("error", result_str)
                            except Exception:
                                pass
                        elif result_str.startswith("(") and result_str.endswith(")"):
                            _clean_err = result_str[1:-1]
                        
                        # Strip technical prefixes for user-friendly message
                        if _clean_err.startswith("MCP tool call "):
                            _clean_err = _clean_err.replace("MCP tool call ", "")
                        
                        await on_progress(f"⚠️ {tool_call.name}: {_clean_err}")

                    if tool_call.name.startswith("mcp_"):
                        if _parsed is not None:
                            if isinstance(_parsed, dict) and isinstance(_parsed.get("data"), list):
                                _summary = f"{_parsed.get('total', len(_parsed['data']))} rows"
                            elif isinstance(_parsed, list):
                                _summary = f"{len(_parsed)} rows"
                            else:
                                _summary = f"{len(result_str)} chars"
                        else:
                            _summary = f"{len(result_str)} chars"
                        logger.info("[iter {}] ← MCP {} | {}ms | {}", iteration, tool_call.name, tool_ms, _summary)
                        # Debug: log full MCP response
                        logger.debug("[iter {}] MCP {} RESPONSE: {}", iteration, tool_call.name, result_str[:1000].replace("\n", " "))
                        mcp_tools_called_this_turn.add(tool_call.name)
                        
                        # Stop immediately on any MCP error — no retries
                        if is_error:
                            error_code, error_message = self._extract_mcp_error(result_str, _parsed)
                            logger.error(
                                "[iter {}] MCP error — stopping loop. tool={} code={} message={}",
                                iteration, tool_call.name, error_code, error_message,
                            )
                            final_content = f"❌ **{error_code}**: {error_message}"
                            # Record the error result in messages for session history
                            messages = self.context.add_tool_result(
                                messages, tool_call.id, tool_call.name, result_str
                            )
                            break  # breaks inner for-loop; outer loop guard below
                    else:
                        logger.info(
                            "[iter {}] ← {} | {}ms | {}",
                            iteration, tool_call.name, tool_ms,
                            result_str[:120].replace("\n", " "),
                        )

                    # Check if MCP server already assigned a resultset_id
                    _mcp_resultset_id: str | None = None
                    if tool_call.name.startswith("mcp_") and isinstance(_parsed, dict):
                        _mcp_resultset_id = (
                            _parsed.get("resultset_id")
                            or _parsed.get("result_id")
                            or _parsed.get("id")
                            if _parsed.get("resultset_id") or _parsed.get("result_id")
                            else None
                        )
                    if _mcp_resultset_id:
                        _latest_mcp_resultset_id = _mcp_resultset_id

                    _classification = classify_json_result(
                        tool_call.name, result_str, parsed=_parsed
                    )
                    _result_kind = (
                        get_result_kind(result_str, parsed=_parsed)
                        if _classification is not None
                        else "management_list"
                    )

                    _already_compact = (
                        _mcp_resultset_id is not None
                        and isinstance(_parsed, dict)
                        and (
                            "preview" in _parsed
                            or (isinstance(_parsed.get("data"), list) and len(_parsed["data"]) <= 20)
                        )
                    )
                    if len(result_str) > _IN_LOOP_RESULT_MAX and _classification is not None and not _already_compact:
                        ctx_result = _compact_tabular_result(
                            result_str, parsed=_parsed, result_kind=_result_kind
                        )
                    else:
                        ctx_result = result_str

                    # Generate resultset_ref for contact_company results
                    if (
                        _classification is not None
                        and _result_kind == "contact_company"
                        and tool_call.name not in _EAGER_STORE_EXEMPT
                    ):
                        _resultset_ref = _mcp_resultset_id or f"agent_{_uuid.uuid4().hex[:12]}"
                        try:
                            _compact_data = json.loads(ctx_result)
                            _compact_data["resultset_ref"] = _resultset_ref
                            ctx_result = json.dumps(_compact_data, ensure_ascii=False)
                        except Exception:
                            pass
                        # Extract human-readable label from MCP response for multi-dataset context
                        _shape, _row_count = _classification
                        _label = make_discovery_label(tool_call.name, _shape, _row_count, result_str)
                        if isinstance(_parsed, dict):
                            _seg_name = _parsed.get("segmentName") or _parsed.get("query")
                            if _seg_name:
                                _label = f"{_seg_name} ({_row_count} rows)"
                        self._push_session_resultset(
                            session_key, _resultset_ref, _label, _row_count, _result_kind
                        )
                        # Schedule label embedding (non-blocking)
                        if memory_store_ref and hasattr(memory_store_ref, "index_resultset_label"):
                            _fields: list[str] = []
                            try:
                                _sample = json.loads(result_str)
                                if isinstance(_sample, list) and _sample:
                                    _fields = list(_sample[0].keys())[:10]
                                elif (
                                    isinstance(_sample, dict)
                                    and isinstance(_sample.get("data"), list)
                                    and _sample["data"]
                                ):
                                    _fields = list(_sample["data"][0].keys())[:10]
                            except Exception:
                                pass
                            asyncio.create_task(memory_store_ref.index_resultset_label(
                                resultset_ref=_resultset_ref,
                                label=_label,
                                fields=_fields,
                                row_count=_row_count,
                                account_id=account_id,
                                user_id=user_id,
                                session_id=session_key,
                            ))
                        resultset_refs[tool_call.id] = _resultset_ref

                    if ctx_result is not result_str:
                        raw_payloads[tool_call.id] = result_str
                    # Track the last MCP execute result for webui JSON stitching.
                    # Stored under a sentinel key so _process_message can retrieve it.
                    if (
                        tool_call.name.endswith("_execute")
                        and not is_error
                        and isinstance(_parsed, dict)
                    ):
                        raw_payloads["_mcp_execute_latest"] = result_str
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, ctx_result
                    )

                # If an MCP error stopped the inner loop, propagate to outer loop too.
                if final_content is not None:
                    break

                # Track progress: at least one tool succeeded this iteration
                if any(not r.startswith('{"error"') and not r.startswith("(")
                       for r in [messages[-1].get("content", "")] if r):
                    no_progress_iterations = 0
                else:
                    # Check if ALL tools in this iteration failed
                    _all_failed = all(
                        msg.get("content", "").startswith('{"error"') or 
                        msg.get("content", "").startswith("(")
                        for msg in messages[-len(response.tool_calls):]
                        if msg.get("role") == "tool"
                    )
                    if _all_failed:
                        no_progress_iterations += 1
                        if no_progress_iterations >= max_no_progress:
                            logger.error("[iter {}] Stopping: {} iterations with no progress", iteration, no_progress_iterations)
                            final_content = (
                                f"⚠️ **No progress after {no_progress_iterations} iterations**\n\n"
                                f"I've tried multiple approaches but all tool calls are failing. This usually means:\n"
                                f"1. The requested API endpoint doesn't exist\n"
                                f"2. The search tool couldn't find a relevant workflow\n"
                                f"3. The request needs more context\n\n"
                                f"**Next steps**: Rephrase your request or ask 'what can you do?' to see available capabilities."
                            )
                            break

            else:
                clean = self._strip_think(response.content)
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages, clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        total_ms = int((time.monotonic() - loop_start) * 1000)
        logger.info(
            "Agent done: {} iter {}ms | tools: {}",
            iteration, total_ms,
            ", ".join(tools_used) if tools_used else "(none)",
        )
        _telemetry.capture("agent.loop_complete", {
            "session_key": session_key,
            "iterations": iteration,
            "total_ms": total_ms,
            "tools_used": list(set(tools_used)),
        }, account_id=account_id)

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        # Safety net: if classification missed a resultset_id (e.g. "preview" envelope
        # misclassified as management_list), still surface it so _active_resultsets
        # gets updated even when resultset_refs is empty.
        if _latest_mcp_resultset_id and not resultset_refs:
            resultset_refs[f"_mcp_fallback_{_latest_mcp_resultset_id}"] = _latest_mcp_resultset_id

        return final_content, tools_used, messages, raw_payloads, resultset_refs

    @staticmethod
    def _parse_agent_json_response(text: str) -> dict | None:
        """Try to parse a JSON-structured agent response.

        Handles raw JSON or ```json ... ``` fences. Returns the parsed dict
        if it contains a "meta" key, otherwise None.
        """
        if not text:
            return None
        stripped = text.strip()
        # Strip optional ```json ... ``` fences
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*\n?", "", stripped)
            stripped = re.sub(r"\n?```\s*$", "", stripped).strip()
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "meta" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks."""
        self._running = True
        await self._connect_mcp()
        await self._ensure_db()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._remove_active_task(k, t))

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under a per-session lock."""
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        async with lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def close(self) -> None:
        """Close the agent loop and its managed resources."""
        self.stop()
        for task in self._consolidation_tasks:
            task.cancel()
        if self._consolidation_tasks:
            await asyncio.gather(*self._consolidation_tasks, return_exceptions=True)
            self._consolidation_tasks.clear()
        try:
            await self.close_mcp()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error closing MCP")

        # Close Postgres pool
        try:
            from nanobot.db import connection as db_connection
            await db_connection.close_pool()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error closing Postgres pool")

    def _remove_active_task(self, session_key: str, task: asyncio.Task) -> None:
        tasks = self._active_tasks.get(session_key)
        if tasks and task in tasks:
            tasks.remove(task)

    def _get_consolidation_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._consolidation_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._consolidation_locks[session_key] = lock
        return lock

    def _prune_consolidation_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        if not lock.locked():
            self._consolidation_locks.pop(session_key, None)

    async def _get_account_dna(self, account_id: str) -> dict | None:
        """Fetch company DNA for an account, using an in-process cache.

        Returns None when DB is unavailable or no DNA has been configured.
        The cache is never invalidated mid-process; restart to pick up changes.
        """
        if account_id in self._account_dna_cache:
            cached = self._account_dna_cache[account_id]
            if cached:
                logger.debug("[DNA] cache hit for account '{}' — company: {}", account_id, cached.get("company_snapshot", {}).get("brand_name", "unknown"))
            else:
                logger.debug("[DNA] cache hit for account '{}' — no DNA configured", account_id)
            return cached or None
        if not self._db_manager:
            logger.warning("[DNA] db_manager not initialised — cannot load DNA for account '{}'", account_id)
            return None
        try:
            dna = await self._db_manager.get_account_dna(account_id)
            self._account_dna_cache[account_id] = dna or {}
            if dna:
                brand = dna.get("company_snapshot", {}).get("brand_name") or dna.get("company_name", "unknown")
                logger.info("[DNA] loaded for account '{}' — company: {}", account_id, brand)
            else:
                logger.info("[DNA] no DNA found for account '{}'", account_id)
            return dna or None
        except Exception as exc:
            logger.warning("[DNA] failed to load for account '{}': {}", account_id, exc)
            return None

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""

        # Ensure DB is ready
        await self._ensure_db()

        # Resolve account_id and user_id from the message or config defaults
        default_account = self.config.agents.defaults.default_account_id
        account_id = (msg.account_id or msg.metadata.get("account_id", "")).strip() or default_account
        user_id = (msg.user_id or msg.metadata.get("user_id", "")).strip() or msg.sender_id

        # Guard: both identifiers are required for multi-tenant DB writes.
        # Return a structured error immediately — never touch the LLM.
        if not account_id or not user_id:
            missing = []
            if not account_id:
                missing.append("account_id")
            if not user_id:
                missing.append("user_id")
            error_text = (
                f"Missing required fields: {', '.join(missing)}. "
                "Each request must include account_id and user_id."
            )
            logger.warning(
                "_process_message rejected: missing {} (channel={}, chat_id={})",
                missing, msg.channel, msg.chat_id,
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=error_text,
                metadata={"error": "missing_identity", "missing_fields": missing},
            )

        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = await self.sessions.get_or_create(
                key, account_id=account_id, user_id=user_id, channel=channel
            )
            self._set_tool_context(
                channel, chat_id, msg.metadata.get("message_id"),
                db_manager=self._db_manager, account_id=account_id, user_id=user_id,
            )
            history = session.get_history(max_messages=self.memory_window)
            history = prepare_history_for_llm(history)
            memory_context = await self.memory_store.get_memory_context(
                key,
                query=msg.content,  # Pass user query for vector search
                account_id=account_id,
                user_id=user_id,
            )
            company_dna = await self._get_account_dna(account_id)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                memory_context=memory_context,
                company_dna=company_dna,
            )
            final_content, _, all_msgs, raw_payloads, resultset_refs = await self._run_agent_loop(
                messages, session_key=key, account_id=account_id, user_id=user_id,
                db_mgr=self._db_manager, memory_store_ref=self.memory_store,
            )
            await self._save_turn(
                session, all_msgs, 1 + len(history),
                raw_payloads=raw_payloads,
                resultset_refs=resultset_refs,
                account_id=account_id, user_id=user_id,
            )
            await self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = await self.sessions.get_or_create(
            key, account_id=account_id, user_id=user_id, channel=msg.channel
        )

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            lock = self._get_consolidation_lock(session.key)
            self._consolidating.add(session.key)
            archival_ok = True
            max_consolidate_attempts = 2
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        for attempt in range(max_consolidate_attempts):
                            if await self._consolidate_memory(
                                temp, archive_all=True,
                                account_id=account_id, user_id=user_id,
                            ):
                                break
                            if attempt < max_consolidate_attempts - 1:
                                await asyncio.sleep(1)
                            else:
                                archival_ok = False
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                archival_ok = False
            finally:
                self._consolidating.discard(session.key)

            session.clear()
            await self.sessions.save(session)
            await self.sessions.invalidate(session.key)
            if archival_ok:
                return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                      content="New session started.")
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started. (Memory archival was skipped after retry; you can continue.)")

        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 nanobot commands:\n/new — Start a new conversation\n/stop — Stop the current task\n/help — Show available commands")

        unconsolidated_msgs = session.messages[session.last_consolidated:]
        unconsolidated = len(unconsolidated_msgs)
        est_chars = sum(len(str(m.get("content") or "")) for m in unconsolidated_msgs)
        should_consolidate = (
            unconsolidated >= self.memory_window
            or est_chars >= 100_000
        )
        if should_consolidate and session.key not in self._consolidating:
            self._consolidating.add(session.key)
            session_lock = self._session_locks.setdefault(session.key, asyncio.Lock())
            _acc_id = account_id
            _usr_id = user_id

            async def _consolidate_and_unlock():
                try:
                    async with session_lock:
                        await self._consolidate_memory(
                            session,
                            account_id=_acc_id, user_id=_usr_id,
                        )
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(
            msg.channel, msg.chat_id, msg.metadata.get("message_id"),
            db_manager=self._db_manager, account_id=account_id, user_id=user_id,
            active_resultset_id=self._active_resultsets.get(key) or "",
        )
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        # Extract synthetic context injected by slash-command prompt handler.
        _meta = msg.metadata or {}
        synthetic_turns: list[dict] = _meta.get("_synthetic_turns") or []
        ephemeral_skill_name: str | None = _meta.get("_ephemeral_skill") or None
        # resultset_ref stored in Agent Postgres by webui; resultset_id is the
        # MCP-server-assigned id (may differ).  Prefer the MCP id when present.
        _synthetic_rs_ref: str = _meta.get("_synthetic_resultset_ref") or ""
        _synthetic_rs_id: str = _meta.get("_synthetic_resultset_id") or ""
        _synthetic_active_rs = _synthetic_rs_id or _synthetic_rs_ref or None

        # Active resultset from previous turns in this session.
        active_resultset_id: str | None = self._active_resultsets.get(key)

        # Restore per-session dataset list from persistent metadata on first access
        # (survives server restarts — saved as session.metadata["datasets"]).
        if key not in self._session_resultsets:
            _persisted = session.metadata.get("datasets")
            if isinstance(_persisted, list):
                self._session_resultsets[key] = _persisted

        # Synthetic injection takes precedence — it is the *newest* dataset.
        if _synthetic_active_rs:
            active_resultset_id = _synthetic_active_rs
            self._active_resultsets[key] = _synthetic_active_rs
            # Register the slash-command result in the session dataset list.
            # webui.py passes pre-computed label + row_count via metadata.
            _syn_label: str = _meta.get("_synthetic_label") or ""
            _syn_rows: int = int(_meta.get("_synthetic_row_count") or 0)
            if not _syn_label:
                # Fallback: derive label from tool name (e.g. "mcp_segments" → "Segments")
                _syn_tool = next(
                    (_st.get("name", "") for _st in synthetic_turns if _st.get("role") == "tool"), ""
                )
                _syn_label = _syn_tool.replace("mcp_", "", 1).replace("_", " ").title() or "Dataset"
            self._push_session_resultset(key, _synthetic_active_rs, _syn_label, _syn_rows, "management_list")

        # Build resultset_ref mapping for synthetic tool result so _save_turn
        # can store it against the correct tool_call_id in session_messages.
        extra_resultset_refs: dict[str, str] = {}
        if synthetic_turns and _synthetic_rs_ref:
            for _st in synthetic_turns:
                if _st.get("role") == "tool" and _st.get("tool_call_id"):
                    extra_resultset_refs[_st["tool_call_id"]] = _synthetic_rs_ref

        history = session.get_history(max_messages=self.memory_window)
        history = prepare_history_for_llm(history)

        memory_context = await self.memory_store.get_memory_context(
            key, query=msg.content,
            account_id=account_id,
            user_id=user_id,
        )
        company_dna = await self._get_account_dna(account_id)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            memory_context=memory_context,
            synthetic_turns=synthetic_turns or None,
            ephemeral_skill_name=ephemeral_skill_name,
            active_resultset_id=active_resultset_id,
            session_datasets=self._session_resultsets.get(key) or None,
            company_dna=company_dna,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False, streaming: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            meta["_streaming"] = streaming
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        _effective_progress = on_progress or _bus_progress

        _telemetry.capture("agent.message_received", {
            "session_key": key,
            "channel": msg.channel,
            "message_length": len(msg.content),
        }, account_id=account_id)

        if (intent_tool := self.tools.get("analyze_enrichment_intent")) and hasattr(intent_tool, "set_context"):
            intent_tool.set_context(
                account_id=account_id,
                on_progress=_effective_progress,
                thinking_translator_model=self.config.agents.defaults.thinking_translator_model,
            )
        final_content, _, all_msgs, raw_payloads, resultset_refs = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            session_key=key,
            account_id=account_id,
            user_id=user_id,
            db_mgr=self._db_manager,
            memory_store_ref=self.memory_store,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        # Merge synthetic resultset refs with any refs generated by the LLM loop.
        merged_resultset_refs = {**extra_resultset_refs, **resultset_refs}

        await self._save_turn(
            session, all_msgs, 1 + len(history),
            raw_payloads=raw_payloads,
            resultset_refs=merged_resultset_refs,
            account_id=account_id, user_id=user_id,
        )

        # Persist synthetic active resultset so it survives server restarts.
        if _synthetic_active_rs:
            session.metadata["active_resultset_id"] = _synthetic_active_rs
            logger.debug(
                "Active resultset set from slash command: session={}, rs={}",
                key, _synthetic_active_rs,
            )
        # Override with any resultset generated by LLM tool calls this turn.
        # Use resultset_refs (LLM-generated only), not merged_resultset_refs —
        # merged includes extra_resultset_refs which holds the agent_ DB key from
        # the slash-command synthetic turn, not the MCP-canonical ID.
        if resultset_refs:
            _latest = list(resultset_refs.values())[-1]
            self._active_resultsets[key] = _latest
            session.metadata["active_resultset_id"] = _latest

        # Persist the full dataset list so it survives server restarts.
        _current_datasets = self._session_resultsets.get(key)
        if _current_datasets:
            session.metadata["datasets"] = _current_datasets

        await self.sessions.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        # Forward active_resultset_id so the webui can route #download-csv correctly.
        resp_meta = {k: v for k, v in (msg.metadata or {}).items()
                     if not k.startswith("_")}
        if active_rs := session.metadata.get("active_resultset_id"):
            resp_meta["active_resultset_id"] = active_rs
        # Build structured response for webui channel.
        if msg.channel == "webui":
            structured = self._parse_agent_json_response(final_content)
            if structured is not None:
                # MCP payload source priority:
                #   1. raw_payloads["_mcp_execute_latest"] — set when agent called
                #      an MCP execute tool inside _run_agent_loop.
                #   2. msg.metadata["_mcp_execute_latest"] — injected by webui.py
                #      for slash-command fast-path (MCP called directly).
                latest_mcp_str = (
                    raw_payloads.get("_mcp_execute_latest")
                    or (msg.metadata or {}).get("_mcp_execute_latest")
                )
                if latest_mcp_str:
                    # MCP payload is the authoritative data source.
                    # Extract only meta.next_actions from the LLM response —
                    # discard any text/table the LLM generated so the MCP JSON
                    # flows through to the UI intact.
                    try:
                        mcp_payload = json.loads(latest_mcp_str)
                        resp_meta["structured_response"] = {
                            **mcp_payload,
                            "meta": structured.get("meta", {}),
                        }
                    except Exception:
                        resp_meta["structured_response"] = structured
                else:
                    # No MCP payload — conversational response, use as-is.
                    resp_meta["structured_response"] = structured
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=resp_meta,
        )

    async def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        raw_payloads: dict[str, str] | None = None,
        resultset_refs: dict[str, str] | None = None,
        account_id: str = "",
        user_id: str = "",
    ) -> None:
        """Save new-turn messages into Postgres session_messages and in-memory session."""
        from datetime import datetime

        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages
            # Skip synthetic slash-command turns — they are scaffolding injected at
            # runtime and must not be persisted. If saved, they reappear in the next
            # call's history window as orphaned tool messages (no matching assistant
            # tool_calls), causing strict LLMs (e.g. xAI Grok) to reject the payload.
            tool_call_id = entry.get("tool_call_id", "")
            if isinstance(tool_call_id, str) and tool_call_id.startswith("synth_"):
                continue
            if role == "assistant":
                synth_calls = [
                    c for c in (entry.get("tool_calls") or [])
                    if isinstance(c.get("id"), str) and c["id"].startswith("synth_")
                ]
                if synth_calls:
                    continue
            if role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered

            entry.setdefault("timestamp", datetime.now().isoformat())

            if self._db_manager is not None:
                _role = entry.get("role", "user")
                _content_val = entry.get("content")
                _tool_name = entry.get("name")
                _tool_call_id = entry.get("tool_call_id", "")
                _resultset_ref = (resultset_refs or {}).get(_tool_call_id) if _role == "tool" else None

                # Build the content jsonb object (full raw message dict)
                content_dict = dict(entry)

                # For tool messages, store full payload if available
                if _role == "tool" and isinstance(_content_val, str):
                    full_payload = (raw_payloads or {}).get(_tool_call_id, _content_val)
                    content_dict["content"] = full_payload

                try:
                    await self._db_manager.insert_message(
                        session_id=session.key,
                        account_id=account_id,
                        user_id=user_id,
                        role=_role,
                        content_dict=content_dict,
                        tool_name=_tool_name,
                        resultset_ref=_resultset_ref,
                    )
                except Exception as exc:
                    logger.warning("Failed to insert message into Postgres: {}", exc)

                # Append to in-memory session
                raw_data = dict(entry)
                session.messages.append(raw_data)
                session.updated_at = datetime.now()
            else:
                # DB not initialised — store in memory only
                if role == "tool" and isinstance(content, str):
                    if len(content) > self._TOOL_RESULT_MAX_CHARS:
                        entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                session.messages.append(entry)

        session.updated_at = datetime.now()

    async def _consolidate_memory(
        self,
        session: "Session",
        archive_all: bool = False,
        *,
        account_id: str = "",
        user_id: str = "",
    ) -> bool:
        """Delegate to HybridMemoryStore.consolidate(). Returns True on success."""
        if self.memory_store is None:
            return False
        return await self.memory_store.consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
            account_id=account_id, user_id=user_id,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""
