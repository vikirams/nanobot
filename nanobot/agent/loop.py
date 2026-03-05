"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import weakref
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.history_prep import (
    is_tabular_tool_result,
    classify_json_result,
    make_discovery_label,
    prepare_history_for_llm,
)
from nanobot.agent.tools.compute import AnalyzeDiscoveryTool
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.dedup import DeduplicateTool
from nanobot.agent.tools.discovery import (
    ExportDiscoveryToCsvTool,
    GetDiscoveryDataTool,
    ListDiscoveryResultsTool,
)
from nanobot.agent.tools.export import SaveCSVTool
from nanobot.agent.tools.intent import IntentAnalysisTool
from nanobot.agent.tools.webhook import WebhookPushTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

from nanobot.hybrid_memory.stores import HybridMemoryStore, HybridSessionManager
from nanobot.hybrid_memory.sqlite_manager import SqliteManager, get_account_db_path
from nanobot.hybrid_memory.zvec_manager import ZvecManager, get_account_zvec_path

from nanobot.config.schema import Config, ExecToolConfig
from nanobot import telemetry as _telemetry

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig
    from nanobot.cron.service import CronService

# Tools whose results are metadata/summaries — never store as discovery results.
_EAGER_STORE_EXEMPT: frozenset[str] = frozenset({
    "analyze_enrichment_intent",
    "list_discovery_results",
    "get_discovery_data",
    "deduplicate_results",
    "export_discovery_to_csv",
    "push_to_webhook",
    "save_csv",
    "analyze_discovery_data",
})


def _compact_tabular_result(result_str: str, max_preview_rows: int = 20, parsed: Any = None) -> str:
    """Compact a large tabular JSON tool result for in-loop context.

    Rebuilds the payload keeping only the first `max_preview_rows` rows so the agent
    can display a preview table without blowing the context window. The full payload is
    still persisted by _save_turn via the original uncompacted result.
    If parsed is provided (already-loaded JSON), skips json.loads.
    """
    try:
        data = json.loads(result_str) if parsed is None else parsed
        _NO_SAVE_NOTE = (
            " Full {total} rows stored. "
            "DO NOT call save_csv or export_discovery_to_csv — show the preview table and export buttons "
            "(📥 Download CSV / 📤 Push to Segment / 🔗 Push to Webhook). "
            "For analysis, use analyze_discovery_data with Python code (reads full dataset via sys.stdin)."
        )
        if isinstance(data, list) and data and isinstance(data[0], dict):
            total = len(data)
            preview = data[:max_preview_rows]
            return json.dumps({
                "total": total,
                "preview_rows": len(preview),
                "note": f"Showing first {len(preview)} of {total} rows for preview." + _NO_SAVE_NOTE.format(total=total),
                "data": preview,
            })
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            rows = data["data"]
            total = data.get("total", len(rows))
            preview = rows[:max_preview_rows]
            compacted = {k: v for k, v in data.items() if k != "data"}
            compacted["preview_rows"] = len(preview)
            compacted["note"] = f"Showing first {len(preview)} of {total} rows for preview." + _NO_SAVE_NOTE.format(total=total)
            compacted["data"] = preview
            return json.dumps(compacted)
    except Exception:
        pass
    # Fallback: hard truncation with a note
    return result_str[:6_000] + "\n[Result truncated — full payload stored as discovery dataset]"


def _classify_entity_type(record: dict) -> str | None:
    """Heuristically classify a discovery record as 'contact' or 'company'."""
    contact_keys = {"email", "first_name", "last_name", "title", "job_title", "full_name"}
    company_keys = {"domain", "website", "founded", "employees", "headcount", "company_name"}
    if contact_keys.intersection(record):
        return "contact"
    linkedin = str(record.get("linkedin_url") or "")
    if "/in/" in linkedin:
        return "contact"
    if "/company/" in linkedin:
        return "company"
    if company_keys.intersection(record):
        return "company"
    return None


async def _upsert_entities_from_payload(
    payload: str,
    session_key: str,
    sqlite_mgr: "SqliteManager",
    cap: int = 500,
) -> None:
    """Extract entity records from a JSON tool result and upsert into the entities table.

    Capped at *cap* records per tool call to prevent runaway writes on huge payloads.
    Failures are intentionally swallowed by the caller so a bad payload never breaks
    the main agent turn.
    """
    import json as _json

    try:
        data = _json.loads(payload)
    except Exception:
        return

    records: list[dict] = []
    if isinstance(data, list):
        records = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict) and isinstance(data.get("data"), list):
        records = [r for r in data["data"] if isinstance(r, dict)]

    for rec in records[:cap]:
        etype = _classify_entity_type(rec)
        if not etype:
            continue
        await sqlite_mgr.upsert_entity(
            entity_type=etype,
            data=rec,
            session_id=session_key,
            email=str(rec.get("email") or "").strip() or None,
            linkedin_url=str(rec.get("linkedin_url") or "").strip() or None,
            domain=str(rec.get("domain") or rec.get("website") or "").strip() or None,
        )


class _AccountManagers:
    """Per-account hybrid memory managers (SQLite + zvec + session + memory store)."""

    __slots__ = ("sqlite", "zvec", "sessions", "memory")

    def __init__(
        self,
        sqlite: SqliteManager,
        zvec: ZvecManager,
        sessions: HybridSessionManager,
        memory: HybridMemoryStore,
    ) -> None:
        self.sqlite = sqlite
        self.zvec = zvec
        self.sessions = sessions
        self.memory = memory


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
        config: Config,  # Added config parameter
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None, # Still allow injection if needed
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
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

        # Initialise memory/session backends FIRST — ContextBuilder depends on nothing,
        # but _process_message needs self.memory_store and self.sessions to exist.
        if config.enable_hybrid_memory:
            logger.debug("Hybrid memory enabled")
            # Global/anonymous managers (no account_id) — shared by all sessions that
            # arrive without an account_id.  A single SqliteManager is shared by both
            # HybridSessionManager and HybridMemoryStore so there is exactly one
            # connection to the global workspace database.
            self.sqlite_manager: SqliteManager | None = SqliteManager(workspace)
            self.zvec_manager: ZvecManager | None = ZvecManager(workspace, provider=self.provider)
            self.sessions: SessionManager | HybridSessionManager = HybridSessionManager(
                workspace, sqlite_manager=self.sqlite_manager
            )
            self.memory_store: MemoryStore | HybridMemoryStore = HybridMemoryStore(
                workspace, self.sqlite_manager, self.zvec_manager, provider=self.provider
            )
            # Per-account manager registry: account_id → _AccountManagers.
            # Each account gets its own DB (~/.nanobot/accounts/<id>/workspace.db)
            # and zvec collection (~/.nanobot/accounts/<id>/zvec/), ensuring full
            # storage and memory isolation between tenants.
            self._account_managers: dict[str, _AccountManagers] = {}
            self._account_lock = asyncio.Lock()
        else:
            logger.debug("Hybrid memory disabled")
            self.sqlite_manager = None
            self.zvec_manager = None
            self.sessions = session_manager or SessionManager(workspace)
            self.memory_store = MemoryStore(workspace)
            self._account_managers = {}
            self._account_lock = asyncio.Lock()

        self.context = ContextBuilder(workspace, hybrid_memory_enabled=config.enable_hybrid_memory)
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
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        # Per-session processing locks so concurrent sessions are never serialised
        # behind a single global lock.  In asyncio's cooperative model, dict.setdefault
        # is atomic between await points, so no additional mutex is required.
        self._session_locks: dict[str, asyncio.Lock] = {}
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
        self.tools.register(SaveCSVTool(workspace=self.workspace))
        # Intent analysis — use a dedicated fast GROQ provider when configured,
        # otherwise fall back to the main provider/model for backward compat.
        intent_provider = self.provider
        intent_model = self.model
        _intent_cfg = self.config.agents.defaults.intent_model
        _groq_key = (self.config.providers.groq.api_key.get_secret_value() or "").strip()
        if _intent_cfg and _groq_key:
            from nanobot.providers.litellm_provider import LiteLLMProvider
            intent_provider = LiteLLMProvider(
                api_key=_groq_key,
                default_model=_intent_cfg,
                provider_name="groq",
            )
            intent_model = _intent_cfg
            logger.debug("Intent analysis using GROQ model: {}", intent_model)
        self.tools.register(IntentAnalysisTool(provider=intent_provider, model=intent_model))
        if self.config.enable_hybrid_memory and self.sqlite_manager:
            self.tools.register(ListDiscoveryResultsTool(sqlite_manager=self.sqlite_manager))
            self.tools.register(
                ExportDiscoveryToCsvTool(
                    sqlite_manager=self.sqlite_manager,
                    workspace=self.workspace,
                )
            )
            self.tools.register(GetDiscoveryDataTool(sqlite_manager=self.sqlite_manager))
            self.tools.register(DeduplicateTool(sqlite_manager=self.sqlite_manager))
            self.tools.register(WebhookPushTool(sqlite_manager=self.sqlite_manager))
            self.tools.register(AnalyzeDiscoveryTool(
                sqlite_manager=self.sqlite_manager,
                sandbox_config=self.config.sandbox,
            ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

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
            # CancelledError is BaseException in Python 3.8+ and is NOT caught by
            # `except Exception`. The MCP transport layer (e.g. streamable_http_client)
            # can raise CancelledError internally during connection setup, which would
            # propagate uncaught and crash agent.run().
            # Only re-raise if this task was actually cancelled (e.g. via SIGTERM);
            # otherwise treat it as a transient connection failure and retry later.
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                # Real task cancellation — skip await-based cleanup to avoid masking it.
                self._mcp_stack = None
                raise
            logger.warning("MCP connection interrupted (spurious CancelledError from transport layer); will retry on next message")
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
        message_id: str | None = None,
        sqlite_manager: Any = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        session_key = f"{channel}:{chat_id}"
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

        for name in (
            "list_discovery_results",
            "export_discovery_to_csv",
            "get_discovery_data",
            "deduplicate_results",
            "push_to_webhook",
            "analyze_discovery_data",
        ):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    # Pass the effective (per-account or global) sqlite_manager so
                    # the tools read from the same DB where results were stored.
                    tool.set_context(session_key, sqlite_manager=sqlite_manager)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    # Sliding window size for duplicate tool call detection.
    # A tool call with the same name + arguments within the last N calls is considered a loop.
    _DUPLICATE_WINDOW = 3

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        session_key: str = "",
        account_id: str = "",
        sqlite_mgr: "SqliteManager | None" = None,
    ) -> tuple[str | None, list[str], list[dict], dict[str, str], set[str]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages, raw_payloads, eagerly_stored_ids)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        loop_start = time.monotonic()
        # Track MCP tools called this turn to block proactive save_csv calls
        mcp_tools_called_this_turn: set[str] = set()
        # Full (uncompacted) payloads keyed by tool_call_id — passed to _save_turn
        # so SQLite always stores the complete dataset, not the in-loop compacted preview.
        raw_payloads: dict[str, str] = {}
        # tool_call_ids already persisted to discovery_results in this loop iteration,
        # so _save_turn can skip them and avoid duplicates.
        eagerly_stored_ids: set[str] = set()

        # Sliding window of recent tool signatures for duplicate detection.
        # Signature = tool_name + stable hash of arguments (catches identical repeated calls).
        recent_tool_sigs: list[str] = []
        tool_defs = self.tools.get_definitions()

        while iteration < self.max_iterations:
            iteration += 1

            # ── LLM call ──────────────────────────────────────────────────────
            logger.info("[iter {}] LLM call ({} msgs)", iteration, len(messages))
            llm_t0 = time.monotonic()

            # Enable token streaming for final-answer turns (no tools in definitions
            # would always stream, but we only stream when on_progress is set so the
            # user sees tokens as they arrive instead of waiting for the full response).
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
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
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

                # ── Duplicate tool call detection (all calls in this batch) ───
                duplicate_detected = False
                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    sig = f"{tool_call.name}:{hash(args_str)}"
                    if sig in recent_tool_sigs[-self._DUPLICATE_WINDOW:]:
                        logger.warning(
                            "[iter {}] Duplicate tool call detected: {}({}) — breaking loop to avoid infinite repetition",
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
                    # Add synthetic tool responses so history stays valid for the API:
                    # an assistant message with tool_calls must be followed by a tool
                    # message per tool_call_id, or the next request returns 400.
                    _skip_content = (
                        "Skipped: duplicate call to avoid infinite loop. "
                        "Present the discovery preview and export buttons from the previous result if available."
                    )
                    for tc in response.tool_calls:
                        messages = self.context.add_tool_result(
                            messages, tc.id, tc.name, _skip_content
                        )
                    break

                # Block save_csv when any tool in this batch is MCP (same-turn rule)
                block_save_csv = any(tc.name.startswith("mcp_") for tc in response.tool_calls)
                save_csv_block_msg = (
                    "ERROR: save_csv must not be called automatically after discovery. "
                    "Present the preview table from the compacted result and show these export buttons:\n"
                    "📥 [Download CSV](#download-csv)\n"
                    "📤 [Push to Segment](#push-to-segment)\n"
                    "🔗 [Push to Webhook](#push-to-webhook)\n"
                    "The user will click a button when they want to export. "
                    "Use get_discovery_data(which='last') if the user asks to analyze the full dataset."
                )

                # Prepare per-call: exec_args, MCP cache key/canonical; then batch cache lookup
                prep: list[tuple[Any, dict, str | None, str | None]] = []
                for tool_call in response.tool_calls:
                    exec_args = tool_call.arguments
                    if (
                        tool_call.name.startswith("mcp_")
                        and isinstance(exec_args, dict)
                        and "page" in exec_args
                    ):
                        exec_args = {k: v for k, v in exec_args.items() if k != "page"}
                    mcp_key: str | None = None
                    mcp_canonical: str | None = None
                    if tool_call.name.startswith("mcp_") and sqlite_mgr is not None:
                        mcp_canonical = json.dumps(exec_args, sort_keys=True, ensure_ascii=False)
                        mcp_key = hashlib.md5(
                            f"{tool_call.name}:{mcp_canonical}".encode()
                        ).hexdigest()
                    prep.append((tool_call, exec_args, mcp_key, mcp_canonical))

                async def _mcp_cache_get(key: str | None) -> str | None:
                    if key is None or sqlite_mgr is None:
                        return None
                    try:
                        return await sqlite_mgr.get_mcp_cache(key)
                    except Exception as _ce:
                        logger.warning("MCP cache lookup failed: {}", _ce)
                        return None

                mcp_cached_list = await asyncio.gather(
                    *[_mcp_cache_get(p[2]) for p in prep]
                )

                async def _run_one_tool(
                    tool_call: Any,
                    exec_args: dict,
                    mcp_cache_key: str | None,
                    mcp_params_canonical: str | None,
                    mcp_cached: str | None,
                ) -> tuple[Any, str, int, str | None, str | None, bool]:
                    if tool_call.name == "save_csv" and block_save_csv:
                        logger.warning(
                            "[iter {}] Blocking proactive save_csv — MCP discovery in same batch",
                            iteration,
                        )
                        return (tool_call, save_csv_block_msg, 0, mcp_cache_key, mcp_params_canonical, False)
                    if mcp_cached is not None:
                        return (tool_call, mcp_cached, 0, mcp_cache_key, mcp_params_canonical, True)
                    t0 = time.monotonic()
                    result = await self.tools.execute(tool_call.name, exec_args)
                    ms = int((time.monotonic() - t0) * 1000)
                    return (tool_call, str(result), ms, mcp_cache_key, mcp_params_canonical, False)

                # Log outbound tool calls
                for idx, (tool_call, exec_args, mcp_key, mcp_canonical) in enumerate(prep):
                    args_str = json.dumps(exec_args, ensure_ascii=False)
                    mcp_cached = mcp_cached_list[idx] if idx < len(mcp_cached_list) else None
                    if tool_call.name.startswith("mcp_"):
                        if mcp_cached is not None:
                            logger.info(
                                "[iter {}] MCP cache HIT: {} ({}B)",
                                iteration, tool_call.name, len(mcp_cached),
                            )
                        mcp_args_log = " | ".join(
                            f"query={str(exec_args[k])[:80]!r}" if k == "query"
                            else f"{k}={exec_args[k]}"
                            for k in ("type", "query", "limit") if k in exec_args
                        )
                        _cache_tag = " [CACHED]" if mcp_cached else ""
                        logger.info("[iter {}] → MCP {}{} | {}", iteration, tool_call.name, _cache_tag, mcp_args_log)
                    else:
                        logger.info("[iter {}] → {} | {}", iteration, tool_call.name, args_str[:150])
                    _telemetry.capture("agent.tool_called", {
                        "session_key": session_key,
                        "iteration": iteration,
                        "tool_name": tool_call.name,
                        "cache_hit": mcp_cached is not None,
                    }, account_id=account_id)

                # Execute all tools in parallel
                run_tasks = [
                    _run_one_tool(
                        p[0], p[1], p[2], p[3],
                        mcp_cached_list[i] if i < len(mcp_cached_list) else None,
                    )
                    for i, p in enumerate(prep)
                ]
                batch_results = await asyncio.gather(*run_tasks)

                # Process results in order (parse once, cache set, telemetry, compact, add message)
                for (tool_call, result_str, tool_ms, _mcp_cache_key, _mcp_params_canonical, was_cached) in batch_results:
                    _parsed: Any = None
                    try:
                        _parsed = json.loads(result_str)
                    except Exception:
                        pass
                    if not was_cached and _mcp_cache_key is not None and sqlite_mgr is not None:
                        try:
                            _is_tab = (
                                isinstance(_parsed, list) and _parsed
                                and isinstance(_parsed[0], dict)
                            ) or (
                                isinstance(_parsed, dict)
                                and isinstance((_parsed or {}).get("data"), list)
                            )
                            if _is_tab:
                                await sqlite_mgr.set_mcp_cache(
                                    _mcp_cache_key, tool_call.name,
                                    _mcp_params_canonical or "", result_str,
                                )
                                logger.info("[iter {}] MCP cache SET: {}", iteration, tool_call.name)
                        except Exception as _se:
                            logger.warning("MCP cache store failed: {}", _se)
                    _telemetry.capture("agent.tool_result", {
                        "session_key": session_key,
                        "iteration": iteration,
                        "tool_name": tool_call.name,
                        "tool_ms": tool_ms,
                        "result_preview": result_str[:300] if result_str else "",
                        "is_error": result_str.startswith('{"error"') if result_str else False,
                        "cache_hit": was_cached,
                    }, account_id=account_id)
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
                        mcp_tools_called_this_turn.add(tool_call.name)
                    else:
                        logger.info(
                            "[iter {}] ← {} | {}ms | {}",
                            iteration, tool_call.name, tool_ms,
                            result_str[:120].replace("\n", " "),
                        )
                    _COMPACT_EXEMPT = frozenset()
                    _IN_LOOP_RESULT_MAX = 8_000
                    ctx_result = (
                        _compact_tabular_result(result_str, parsed=_parsed)
                        if len(result_str) > _IN_LOOP_RESULT_MAX
                        and tool_call.name not in _COMPACT_EXEMPT
                        and classify_json_result(tool_call.name, result_str, parsed=_parsed) is not None
                        else result_str
                    )
                    if ctx_result is not result_str:
                        raw_payloads[tool_call.id] = result_str
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, ctx_result
                    )

                    # Eagerly persist discovery results so /api/preview/latest can
                    # serve data before the LLM streams the [Preview] sentinel.
                    if sqlite_mgr and session_key and tool_call.name not in _EAGER_STORE_EXEMPT:
                        eager_classification = classify_json_result(
                            tool_call.name, result_str, parsed=_parsed,
                        )
                        if eager_classification:
                            _shape, _row_count = eager_classification
                            _label = make_discovery_label(tool_call.name, _shape, _row_count, result_str)
                            try:
                                await sqlite_mgr.insert_discovery_result(
                                    session_id=session_key,
                                    tool_name=tool_call.name,
                                    payload=result_str,
                                    query_or_label=_label,
                                    shape=_shape,
                                    row_count=_row_count,
                                )
                                eagerly_stored_ids.add(tool_call.id)
                            except Exception as _e:
                                logger.warning("Eager discovery persist failed: {}", _e)
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages, clean, reasoning_content=response.reasoning_content,
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

        return final_content, tools_used, messages, raw_payloads, eagerly_stored_ids

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
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
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

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
        """Process a message under a per-session lock.

        Different sessions run concurrently; messages for the SAME session are
        serialised so history and memory state stay consistent.
        """
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
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
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
        if self.config.enable_hybrid_memory:
            try:
                if isinstance(self.memory_store, HybridMemoryStore):
                    await self.memory_store.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error closing HybridMemoryStore")
            try:
                if self.sqlite_manager:
                    await self.sqlite_manager.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error closing SqliteManager")
            try:
                if isinstance(self.sessions, HybridSessionManager):
                    await self.sessions.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error closing HybridSessionManager")

            # Close per-account managers.
            for acct_id, mgrs in list(self._account_managers.items()):
                try:
                    await mgrs.memory.close()
                except Exception:
                    logger.exception("Error closing HybridMemoryStore for account {!r}", acct_id)
                try:
                    await mgrs.sqlite.close()
                except Exception:
                    logger.exception("Error closing SqliteManager for account {!r}", acct_id)
            self._account_managers.clear()

    def _get_consolidation_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._consolidation_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._consolidation_locks[session_key] = lock
        return lock

    def _prune_consolidation_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        """Drop lock entry if no longer in use."""
        if not lock.locked():
            self._consolidation_locks.pop(session_key, None)

    async def _get_account_managers(self, account_id: str) -> _AccountManagers:
        """Return (lazily creating) per-account hybrid memory managers.

        Each account gets:
          • ~/.nanobot/accounts/<account_id>/workspace.db   — SQLite
          • ~/.nanobot/accounts/<account_id>/zvec/          — vector collection
        """
        if account_id in self._account_managers:
            return self._account_managers[account_id]

        async with self._account_lock:
            # Double-checked inside the lock to prevent duplicate init.
            if account_id in self._account_managers:
                return self._account_managers[account_id]

            db_path = get_account_db_path(account_id)
            zvec_path = get_account_zvec_path(account_id)

            sqlite = SqliteManager(self.workspace, db_path=db_path)
            zvec = ZvecManager(self.workspace, provider=self.provider, zvec_path=zvec_path)
            sessions = HybridSessionManager(self.workspace, sqlite_manager=sqlite)
            memory = HybridMemoryStore(self.workspace, sqlite, zvec, provider=self.provider)

            mgrs = _AccountManagers(sqlite=sqlite, zvec=zvec, sessions=sessions, memory=memory)
            self._account_managers[account_id] = mgrs
            logger.debug("Per-account managers created for account {!r}", account_id)
            return mgrs

    async def _resolve_managers(
        self, account_id: str
    ) -> tuple[SessionManager | HybridSessionManager, MemoryStore | HybridMemoryStore, SqliteManager | None]:
        """Return (sessions, memory_store, sqlite_manager) for the given account.

        • If hybrid memory is enabled and account_id is non-empty → per-account managers.
        • Otherwise → global/anonymous managers (backward compatible).
        """
        if self.config.enable_hybrid_memory and account_id:
            mgrs = await self._get_account_managers(account_id)
            return mgrs.sessions, mgrs.memory, mgrs.sqlite
        return self.sessions, self.memory_store, self.sqlite_manager

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            account_id = msg.metadata.get("account_id", "").strip()
            workspace_key = f"workspace_{account_id}" if account_id else "__workspace__"
            sessions_mgr, memory_store, sqlite_mgr = await self._resolve_managers(account_id)
            session = await sessions_mgr.get_or_create(key)
            session.metadata.setdefault("workspace_key", workspace_key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"), sqlite_manager=sqlite_mgr)
            history = session.get_history(max_messages=self.memory_window)
            history = prepare_history_for_llm(history)
            memory_context = await memory_store.get_memory_context(key, workspace_key=workspace_key)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
                memory_context=memory_context,
            )
            final_content, _, all_msgs, raw_payloads, eagerly_stored = await self._run_agent_loop(
                messages, session_key=key, account_id=account_id, sqlite_mgr=sqlite_mgr,
            )
            await self._save_turn(session, all_msgs, 1 + len(history),
                                  sessions_mgr=sessions_mgr, sqlite_mgr=sqlite_mgr,
                                  memory_store=memory_store, raw_payloads=raw_payloads,
                                  eagerly_stored_ids=eagerly_stored)
            await sessions_mgr.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        # Normalise account_id: strip whitespace so "ABC" and "ABC " don't get
        # separate manager instances that collide on the same filesystem path.
        account_id = msg.metadata.get("account_id", "").strip()
        workspace_key = f"workspace_{account_id}" if account_id else "__workspace__"
        sessions_mgr, memory_store, sqlite_mgr = await self._resolve_managers(account_id)
        session = await sessions_mgr.get_or_create(key)
        session.metadata.setdefault("workspace_key", workspace_key)

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
                            if await self._consolidate_memory(temp, archive_all=True,
                                                              memory_store=memory_store):
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
            await sessions_mgr.save(session)
            await sessions_mgr.invalidate(session.key)
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
        # Token-based trigger: estimate ~4 chars/token. Tool-heavy turns (large payloads)
        # can blow context long before reaching memory_window message count.
        # Trigger at 100k chars (~25k tokens) regardless of message count.
        est_chars = sum(len(str(m.get("content") or "")) for m in unconsolidated_msgs)
        should_consolidate = (
            unconsolidated >= self.memory_window  # message-count trigger (existing)
            or est_chars >= 100_000               # token-volume trigger (new)
        )
        if (should_consolidate and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            session_lock = self._session_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with session_lock:
                        await self._consolidate_memory(session, memory_store=memory_store)
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"), sqlite_manager=sqlite_mgr)
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self.memory_window)
        history = prepare_history_for_llm(history)
        memory_context = await memory_store.get_memory_context(key, query=msg.content, workspace_key=workspace_key)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
            memory_context=memory_context,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False, streaming: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            meta["_streaming"] = streaming  # True = individual token delta, False = full chunk
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        # Wire the progress callback into the compute tool so stdout lines stream to the UI.
        _effective_progress = on_progress or _bus_progress
        if (ct := self.tools.get("analyze_discovery_data")) and hasattr(ct, "set_context"):
            ct.set_context(f"{msg.channel}:{msg.chat_id}", on_progress=_effective_progress)

        _telemetry.capture("agent.message_received", {
            "session_key": key,
            "channel": msg.channel,
            "message_length": len(msg.content),
        }, account_id=account_id)

        # Inject account_id and progress callback into intent tool (streams status/tokens to UI).
        if (intent_tool := self.tools.get("analyze_enrichment_intent")) and hasattr(intent_tool, "set_context"):
            intent_tool.set_context(account_id=account_id, on_progress=_effective_progress)

        final_content, _, all_msgs, raw_payloads, eagerly_stored = await self._run_agent_loop(
            initial_messages, on_progress=on_progress or _bus_progress,
            session_key=key, account_id=account_id, sqlite_mgr=sqlite_mgr,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        await self._save_turn(session, all_msgs, 1 + len(history),
                              sessions_mgr=sessions_mgr, sqlite_mgr=sqlite_mgr,
                              memory_store=memory_store, raw_payloads=raw_payloads,
                              eagerly_stored_ids=eagerly_stored)
        await sessions_mgr.save(session)

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=msg.metadata or {},
        )

    async def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        *,
        sessions_mgr: SessionManager | HybridSessionManager | None = None,
        sqlite_mgr: SqliteManager | None = None,
        memory_store: MemoryStore | HybridMemoryStore | None = None,
        raw_payloads: dict[str, str] | None = None,
        eagerly_stored_ids: set[str] | None = None,
    ) -> None:
        """Save new-turn messages into session. Hybrid: persist full content to DB and record discovery results."""
        from datetime import datetime

        from nanobot.hybrid_memory.stores import HybridSessionManager, HybridMemoryStore as _HMS

        # Use caller-supplied managers (per-account) or fall back to global managers.
        effective_sessions = sessions_mgr if sessions_mgr is not None else self.sessions
        effective_sqlite = sqlite_mgr if sqlite_mgr is not None else self.sqlite_manager
        effective_memory = memory_store if memory_store is not None else self.memory_store
        use_hybrid = isinstance(effective_sessions, HybridSessionManager)

        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if role == "tool" and isinstance(content, str):
                if use_hybrid:
                    tool_name_for_storage = entry.get("name") or "unknown"
                    tool_call_id = entry.get("tool_call_id", "")
                    payload_for_storage = (raw_payloads or {}).get(tool_call_id, content)

                    # Skip if already eagerly persisted in _run_agent_loop.
                    already_stored = eagerly_stored_ids and tool_call_id in eagerly_stored_ids
                    classification = (
                        None if already_stored
                        or tool_name_for_storage in _EAGER_STORE_EXEMPT
                        else classify_json_result(tool_name_for_storage, payload_for_storage)
                    )
                    if classification and effective_sqlite:
                        shape, row_count = classification
                        label = make_discovery_label(tool_name_for_storage, shape, row_count, payload_for_storage)
                        try:
                            disc_id = await effective_sqlite.insert_discovery_result(
                                session_id=session.key,
                                tool_name=tool_name_for_storage,
                                payload=payload_for_storage,
                                query_or_label=label,
                                shape=shape,
                                row_count=row_count,
                            )
                            if isinstance(effective_memory, _HMS):
                                workspace_key = session.metadata.get("workspace_key", "__workspace__")
                                await effective_memory.index_discovery_label(label, disc_id, workspace_key)
                        except Exception as e:
                            logger.warning("Failed to store discovery result: {}", e)

                    # Auto-upsert entities into cross-session canonical entity store.
                    if effective_sqlite and tool_name_for_storage not in _EAGER_STORE_EXEMPT:
                        _cls = classification or classify_json_result(tool_name_for_storage, payload_for_storage)
                        if _cls:
                            try:
                                await _upsert_entities_from_payload(
                                    payload_for_storage, session.key, effective_sqlite
                                )
                            except Exception as e:
                                logger.debug("Entity upsert skipped: {}", e)

                elif len(content) > self._TOOL_RESULT_MAX_CHARS:
                    entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            if role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    # Strip the runtime-context prefix, keep only the user text.
                    parts = content.split("\n\n", 1)
                    if len(parts) > 1 and parts[1].strip():
                        entry["content"] = parts[1]
                    else:
                        continue
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                            continue  # Strip runtime context from multimodal messages
                        if (c.get("type") == "image_url"
                                and c.get("image_url", {}).get("url", "").startswith("data:image/")):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())

            if use_hybrid and isinstance(effective_sessions, HybridSessionManager):
                raw_data = dict(entry)
                role_str = raw_data.get("role", "user")
                content_str = raw_data.get("content", "")
                if isinstance(content_str, list):
                    content_str = "[multimodal]"
                await effective_sessions.add_message(
                    session,
                    role_str,
                    content_str,
                    raw_data=raw_data,
                )
            else:
                session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(
        self,
        session,
        archive_all: bool = False,
        *,
        memory_store: MemoryStore | HybridMemoryStore | None = None,
    ) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        effective_memory = memory_store if memory_store is not None else self.memory_store
        return await effective_memory.consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
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
