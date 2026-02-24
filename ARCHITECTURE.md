# nanobot Architecture Document

> **Fork Notice**: This project is a fork of the open-source [HKUDS/nanobot](https://github.com/HKUDS/nanobot) project (MIT License). All extensions and customizations **must preserve the core architecture contracts** described in this document. Do not break the upstream interfaces — doing so prevents future upstream merges and violates the OSS spirit of the fork.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Module Map](#3-module-map)
4. [Core Subsystems — Deep Dive](#4-core-subsystems--deep-dive)
   - 4.1 [CLI Layer](#41-cli-layer-nanobotclicommandspy)
   - 4.2 [Message Bus](#42-message-bus-nanobotbus)
   - 4.3 [Agent Loop](#43-agent-loop-nanobotlagentlooppy)
   - 4.4 [Context Builder](#44-context-builder-nanobotlagentcontextpy)
   - 4.5 [Memory System](#45-memory-system-nanobotlagentmemorypy)
   - 4.6 [Skills System](#46-skills-system-nanobotlagentskillspy)
   - 4.7 [Tool System](#47-tool-system-nanobotlagenttoolss)
   - 4.8 [Session Manager](#48-session-manager-nanobotsessionmanagerpy)
   - 4.9 [Provider Layer](#49-provider-layer-nanobotproviders)
   - 4.10 [Channel Layer](#410-channel-layer-nanobotchannels)
   - 4.11 [Cron Service](#411-cron-service-nanobotcronservicepy)
   - 4.12 [Heartbeat Service](#412-heartbeat-service-nanobotheartbeatservicepy)
   - 4.13 [MCP Integration](#413-mcp-integration-nanobotlagenttools-mcppy)
   - 4.14 [Configuration System](#414-configuration-system-nanobotconfig)
   - 4.15 [WhatsApp Bridge](#415-whatsapp-bridge-bridge)
5. [Data Flow Diagrams](#5-data-flow-diagrams)
6. [Key Design Patterns](#6-key-design-patterns)
7. [Workspace Layout at Runtime](#7-workspace-layout-at-runtime)
8. [Dependency Graph](#8-dependency-graph)
9. [Extension Points for Future Development](#9-extension-points-for-future-development)
10. [Fork Rules — What to Change vs. What to Preserve](#10-fork-rules--what-to-change-vs-what-to-preserve)
11. [Adding New Components — Step-by-Step Guides](#11-adding-new-components--step-by-step-guides)
12. [Security Architecture](#12-security-architecture)
13. [Testing Structure](#13-testing-structure)
14. [Deployment Architecture](#14-deployment-architecture)

---

## 1. Project Overview

**nanobot** is an ultra-lightweight personal AI assistant framework (~4,000 lines of core Python). It is deliberately minimal: every subsystem is a small, readable, composable module.

| Attribute | Value |
|-----------|-------|
| Language | Python ≥ 3.11 |
| Package name | `nanobot-ai` |
| Version (fork base) | 0.1.4 |
| License | MIT |
| Entry point | `nanobot.cli.commands:app` |
| Config location | `~/.nanobot/config.json` |
| Workspace default | `~/.nanobot/workspace/` |
| Primary LLM interface | LiteLLM (+ direct providers) |

**Key design goals (from upstream)**:
- Ultra-lightweight: < 4,000 lines of agent code
- Modular: each subsystem is swappable
- Readable: no magic, no metaprogramming
- Multi-channel: Telegram, Discord, WhatsApp, Feishu, Mochat, DingTalk, Slack, Email, QQ
- Multi-provider: OpenRouter, Anthropic, OpenAI, DeepSeek, Gemini, Qwen, local vLLM, etc.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            nanobot Process                              │
│                                                                         │
│  ┌──────────┐   ┌──────────────────────────────────────────────────┐   │
│  │   CLI    │   │                  Gateway Mode                     │   │
│  │ (typer)  │   │                                                   │   │
│  └────┬─────┘   │  ┌────────┐  ┌──────────┐  ┌──────────────────┐ │   │
│       │         │  │Telegram│  │ Discord  │  │  WhatsApp        │ │   │
│       │         │  │Channel │  │ Channel  │  │  Channel         │ │   │
│       │         │  └───┬────┘  └────┬─────┘  └────┬─────────────┘ │   │
│       │         │      │            │              │               │   │
│       │         │      └────────────┴──────────────┘               │   │
│       │         │                     ▼                             │   │
│       │         │            ┌─────────────────┐                   │   │
│       │         │            │   ChannelManager │                  │   │
│       │         │            └────────┬────────┘                   │   │
│       │         └────────────────────┼────────────────────────────┘   │
│       │                              │ publish_inbound / consume_outbound
│       ▼                              ▼                                  │
│  ┌──────────────────────────────────────────┐                          │
│  │              Message Bus                  │  asyncio.Queue (×2)     │
│  │   [inbound queue] ←→ [outbound queue]    │                          │
│  └─────────────────────┬────────────────────┘                          │
│                         │                                               │
│                         ▼                                               │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │                       Agent Loop                                  │  │
│  │                                                                   │  │
│  │  ContextBuilder  →  LLM Provider  →  Tool Registry               │  │
│  │       │                  ↕                  ↕                    │  │
│  │  MemoryStore        LiteLLM /         read_file, exec,           │  │
│  │  SkillsLoader       Custom /          web_search,                │  │
│  │  SessionManager     OAuth             message, spawn, cron, MCP  │  │
│  │                                            │                     │  │
│  │                                     SubagentManager              │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─────────────────┐  ┌──────────────────┐                             │
│  │  Cron Service   │  │Heartbeat Service  │  (background timers)       │
│  └─────────────────┘  └──────────────────┘                             │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Module Map

```
nanobot/
├── __init__.py            # Version + logo constants
├── __main__.py            # python -m nanobot entry point
│
├── cli/
│   └── commands.py        # All CLI commands (typer app)
│
├── bus/
│   ├── events.py          # InboundMessage, OutboundMessage dataclasses
│   └── queue.py           # MessageBus (two asyncio.Queue instances)
│
├── agent/
│   ├── loop.py            # AgentLoop — main processing engine
│   ├── context.py         # ContextBuilder — system prompt assembly
│   ├── memory.py          # MemoryStore — MEMORY.md + HISTORY.md
│   ├── skills.py          # SkillsLoader — SKILL.md loader
│   ├── subagent.py        # SubagentManager — background task execution
│   └── tools/
│       ├── base.py        # Tool ABC + parameter validation
│       ├── registry.py    # ToolRegistry — dynamic tool management
│       ├── filesystem.py  # read_file, write_file, edit_file, list_dir
│       ├── shell.py       # exec (shell command execution)
│       ├── web.py         # web_search (Brave), web_fetch (readability)
│       ├── message.py     # message (send to channel)
│       ├── spawn.py       # spawn (create subagent)
│       ├── cron.py        # cron (schedule tasks from agent)
│       └── mcp.py         # MCP client + MCPToolWrapper
│
├── channels/
│   ├── base.py            # BaseChannel ABC
│   ├── manager.py         # ChannelManager — route, start/stop all
│   ├── telegram.py        # Telegram via python-telegram-bot
│   ├── discord.py         # Discord via raw WebSocket (gateway v10)
│   ├── whatsapp.py        # WhatsApp via Node.js bridge (WebSocket)
│   ├── feishu.py          # Feishu via lark-oapi WebSocket
│   ├── mochat.py          # Mochat via Socket.IO
│   ├── dingtalk.py        # DingTalk via dingtalk-stream
│   ├── slack.py           # Slack via slack-sdk Socket Mode
│   ├── email.py           # Email via IMAP poll + SMTP reply
│   └── qq.py              # QQ via qq-botpy WebSocket
│
├── providers/
│   ├── base.py            # LLMProvider ABC, LLMResponse, ToolCallRequest
│   ├── registry.py        # PROVIDERS tuple, ProviderSpec dataclass
│   ├── litellm_provider.py # LiteLLMProvider — main multi-provider adapter
│   ├── custom_provider.py  # CustomProvider — direct OpenAI-compatible
│   ├── openai_codex_provider.py # OpenAICodexProvider — OAuth-based
│   └── transcription.py   # Voice transcription (Groq Whisper)
│
├── session/
│   └── manager.py         # Session, SessionManager — JSONL persistence
│
├── config/
│   ├── schema.py          # Pydantic Config model tree
│   └── loader.py          # load_config(), save_config(), migration
│
├── cron/
│   ├── service.py         # CronService — scheduling engine
│   └── types.py           # CronJob, CronSchedule, CronPayload, CronStore
│
├── heartbeat/
│   └── service.py         # HeartbeatService — periodic HEARTBEAT.md check
│
├── skills/                # Built-in skills (SKILL.md files)
│   ├── README.md
│   ├── github/SKILL.md
│   ├── weather/SKILL.md
│   ├── summarize/SKILL.md
│   ├── tmux/SKILL.md
│   ├── clawhub/SKILL.md
│   ├── memory/SKILL.md
│   ├── cron/SKILL.md
│   └── skill-creator/SKILL.md
│
└── utils/
    └── helpers.py         # ensure_dir, safe_filename, get_data_path, etc.

bridge/                    # WhatsApp Node.js bridge (TypeScript)
├── src/
│   ├── index.ts           # Entry point
│   ├── server.ts          # WebSocket server
│   ├── whatsapp.ts        # whatsapp-web.js integration
│   └── types.d.ts         # Type declarations
├── package.json
└── tsconfig.json

workspace/                 # Default workspace template (bootstrapped on onboard)
├── AGENTS.md              # Agent behavior instructions
├── SOUL.md                # Agent personality
├── USER.md                # User profile
├── TOOLS.md               # Tool instructions
├── HEARTBEAT.md           # Periodic tasks
├── SOUL.md
└── memory/
    ├── MEMORY.md          # Long-term facts (LLM-maintained)
    └── HISTORY.md         # Append-only session history log

tests/                     # Test suite (pytest-asyncio)
```

---

## 4. Core Subsystems — Deep Dive

### 4.1 CLI Layer (`nanobot/cli/commands.py`)

**Purpose**: The single entry point for all user interaction. Built with [Typer](https://typer.tiangolo.com/) and [Rich](https://rich.readthedocs.io/).

**Commands**:

| Command | Function | What it does |
|---------|----------|--------------|
| `nanobot onboard` | `onboard()` | Creates `~/.nanobot/config.json`, workspace dir, bootstrap files |
| `nanobot agent [-m "..."]` | `agent()` | Single-shot or interactive chat mode |
| `nanobot gateway` | `gateway()` | Starts agent + channels + cron + heartbeat |
| `nanobot status` | `status()` | Shows config/API key status |
| `nanobot channels login` | `channels_login()` | Starts WhatsApp bridge + QR login |
| `nanobot channels status` | `channels_status()` | Shows channel config table |
| `nanobot cron add/list/remove/enable/run` | cron sub-commands | CRUD on scheduled jobs |
| `nanobot provider login <name>` | `provider_login()` | OAuth authentication flow |

**Provider Factory** (`_make_provider`):
```
Config.get_provider_name(model)
  → "openai_codex" → OpenAICodexProvider
  → "custom"       → CustomProvider(api_key, api_base)
  → anything else  → LiteLLMProvider(api_key, api_base, provider_name, ...)
```

**Interactive Mode Architecture** (as of v0.1.4 — routed through message bus):
```
prompt_toolkit input
  → bus.publish_inbound(InboundMessage)
  → agent.run() consumes inbound → processes → bus.publish_outbound(OutboundMessage)
  → _consume_outbound() reads outbound → prints to console
```
This unification means the CLI interactive mode behaves identically to channels.

**Key implementation details**:
- Uses `prompt_toolkit.PromptSession` with `FileHistory` for persistent CLI history at `~/.nanobot/history/cli_history`
- Terminal state is saved/restored via `termios` to prevent corruption
- Progress streaming via `_progress` metadata flag on outbound messages
- Exit commands: `exit`, `quit`, `/exit`, `/quit`, `:q`, `Ctrl+D`

---

### 4.2 Message Bus (`nanobot/bus/`)

**Purpose**: Decouples all channel implementations from the agent core. A pure asyncio in-process message queue.

```
bus/
  events.py    → InboundMessage, OutboundMessage (dataclasses)
  queue.py     → MessageBus (two asyncio.Queue)
```

**`InboundMessage` fields**:
```python
channel: str        # "telegram", "discord", "cli", "system", etc.
sender_id: str      # Platform user ID
chat_id: str        # Platform chat/channel ID  ← used as routing key
content: str        # Text content
timestamp: datetime
media: list[str]    # Local file paths for media
metadata: dict      # Platform-specific data (thread_ts, message_id, etc.)
session_key: str    # Property: f"{channel}:{chat_id}"
```

**`OutboundMessage` fields**:
```python
channel: str        # Target channel
chat_id: str        # Target chat ID
content: str        # Reply content
reply_to: str|None  # Optional message to reply to
media: list[str]    # Media to send
metadata: dict      # Pass-through (e.g., _progress flag, thread_ts)
```

**Special channel: `"system"`**:
- Used by `SubagentManager` to inject results back into the main loop
- `chat_id` encodes origin as `"original_channel:original_chat_id"` for routing

**Threading model**: Single-process, fully async. No thread safety issues because asyncio is cooperative.

---

### 4.3 Agent Loop (`nanobot/agent/loop.py`)

The heart of nanobot. **One `AgentLoop` instance per process** (gateway mode) or per CLI invocation.

**Constructor parameters** (all from config + bus):
```python
bus              # MessageBus
provider         # LLMProvider (LiteLLM, Custom, Codex)
workspace        # Path to workspace directory
model            # Model string (e.g. "anthropic/claude-opus-4-5")
max_iterations   # Max tool call rounds per message (default: 20)
temperature      # LLM temperature (default: 0.7)
max_tokens       # Max response tokens (default: 8192)
memory_window    # Message window for history (default: 50)
brave_api_key    # For web_search tool
exec_config      # Shell timeout config
cron_service     # CronService reference
restrict_to_workspace  # Sandbox flag
session_manager  # SessionManager
mcp_servers      # dict of MCP server configs
```

**Message processing pipeline** (`_process_message`):

```
InboundMessage arrives from bus
         │
         ├─ channel == "system"? → _process_system_message()
         │
         ├─ content == "/new"?   → clear session + start consolidation task
         ├─ content == "/help"?  → return help text
         │
         ├─ session.messages > memory_window?
         │     → asyncio.create_task(_consolidate_memory())  [background]
         │
         ├─ _set_tool_context(channel, chat_id, message_id)
         │
         ├─ context.build_messages(history, current_message, media)
         │                             ↓
         └─ _run_agent_loop(initial_messages, on_progress)
                   │
                   └─ LLM loop (max_iterations):
                        ├─ provider.chat(messages, tools, model, ...)
                        ├─ has_tool_calls? → execute each → append result → repeat
                        └─ no tool_calls?  → final_content → break
                   │
                   ├─ session.add_message("user", content)
                   ├─ session.add_message("assistant", final_content)
                   ├─ sessions.save(session)
                   │
                   └─ return OutboundMessage
```

**Interim text retry logic**: Some models (e.g. DeepSeek) emit a text response before their first tool call. The loop detects this (no tools used yet + text response) and retries once without forwarding the text.

**`<think>` stripping**: Reasoning tokens from thinking models (DeepSeek-R1, Kimi) enclosed in `<think>...</think>` are stripped before display.

**Progress streaming**: While tool calls are in progress, a `_progress`-flagged `OutboundMessage` is published to the bus so channels/CLI can show intermediate state.

**MCP initialization**: Lazy — connects on first message, not at startup. Uses `AsyncExitStack` for lifecycle management.

**Memory consolidation trigger**: When `len(session.messages) > memory_window`, a background `asyncio.create_task` runs `_consolidate_memory()`. A set `_consolidating` prevents duplicate concurrent consolidations per session.

---

### 4.4 Context Builder (`nanobot/agent/context.py`)

Assembles the full LLM prompt on every turn.

**System prompt structure** (in order):
```
1. Core identity block
   ├─ Role description ("You are nanobot...")
   ├─ Current time + timezone
   ├─ Runtime info (OS, Python version)
   └─ Workspace paths (memory, history, skills)

2. Bootstrap files (if present in workspace/)
   ├─ AGENTS.md  — agent behavior instructions
   ├─ SOUL.md    — personality
   ├─ USER.md    — user profile
   ├─ TOOLS.md   — tool-specific instructions
   └─ IDENTITY.md — custom identity (optional)

3. Memory context
   └─ Contents of workspace/memory/MEMORY.md

4. Active Skills (always=true skills, full content inline)

5. Skills Summary (XML listing of all skills with descriptions + paths)
   → Agent reads SKILL.md via read_file when needed (progressive loading)

6. Current Session info
   └─ Channel + Chat ID appended after system prompt
```

**Message list structure**:
```python
[
  {"role": "system",    "content": system_prompt},
  # ... history messages (from session.get_history()) ...
  {"role": "user",      "content": current_message_or_multipart},
]
```

**Multipart user content** (when media present):
```python
[
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
  {"type": "text",      "text": "user message text"},
]
```

**Bootstrap file order**: `["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]`
Custom identity via `IDENTITY.md` is the intended fork extension point for personality customization.

---

### 4.5 Memory System (`nanobot/agent/memory.py`)

**Two-layer persistence**:

| Layer | File | Purpose | Updated by |
|-------|------|---------|-----------|
| Long-term | `workspace/memory/MEMORY.md` | Structured facts about user, preferences, context | LLM via consolidation |
| History log | `workspace/memory/HISTORY.md` | Append-only timestamped entries, grep-searchable | Consolidation agent |

**Consolidation process** (`AgentLoop._consolidate_memory`):
1. Triggered when session exceeds `memory_window` messages
2. Runs as background asyncio task (non-blocking)
3. Builds a conversation transcript from old messages
4. Calls LLM with a consolidation prompt requesting JSON with:
   - `history_entry`: 2-5 sentence summary to append to HISTORY.md
   - `memory_update`: Updated MEMORY.md content
5. Uses `json_repair` to handle malformed LLM JSON output
6. Tracks `session.last_consolidated` offset to avoid re-processing messages

**`/new` command flow**:
- Copies current messages
- Clears session
- Archives ALL messages via `_consolidate_memory(archive_all=True)` in background task

---

### 4.6 Skills System (`nanobot/agent/skills.py`)

Skills are **Markdown documents** that teach the agent new capabilities. They are not executable code — they are prompt injections.

**Skill file format** (`SKILL.md`):
```markdown
---
name: "github"
description: "Interact with GitHub using the gh CLI"
metadata: '{"nanobot": {"requires": {"bins": ["gh"]}, "always": false}}'
---

# GitHub Skill

Instructions for using the GitHub CLI...
```

**Skill loading priority** (workspace overrides built-in):
```
workspace/skills/<name>/SKILL.md   (highest priority)
nanobot/skills/<name>/SKILL.md     (built-in)
```

**Progressive loading model**:
- Skills with `always=true`: loaded inline in every system prompt
- All other skills: only a summary (name + description + path) in the system prompt
- Agent explicitly calls `read_file` on the SKILL.md path when it needs a skill

**Availability checking**:
```python
requires.bins  → shutil.which() check
requires.env   → os.environ.get() check
```
Unavailable skills are listed in the summary with their missing requirements.

**Built-in skills**:
| Skill | Always | Requires |
|-------|--------|---------|
| `github` | false | `gh` CLI |
| `weather` | false | none |
| `summarize` | false | none |
| `tmux` | false | `tmux` |
| `clawhub` | false | none |
| `memory` | false | none |
| `cron` | false | none |
| `skill-creator` | false | none |

---

### 4.7 Tool System (`nanobot/agent/tools/`)

**Abstract base** (`Tool` ABC):
```python
@property name        → str  (LLM function name)
@property description → str  (shown to LLM)
@property parameters  → dict (JSON Schema)
async execute(**kwargs) → str
```

**Tool registration** (`ToolRegistry`):
- Dict of `{name: Tool}` instances
- `get_definitions()` → list of OpenAI function schema dicts
- `execute(name, params)` → validates params → calls `tool.execute(**params)`
- Parameter validation via `Tool.validate_params()` (JSON Schema subset)

**Built-in tools**:

| Tool class | LLM name | Description |
|-----------|----------|-------------|
| `ReadFileTool` | `read_file` | Read file content |
| `WriteFileTool` | `write_file` | Write/create file |
| `EditFileTool` | `edit_file` | Find-and-replace in file |
| `ListDirTool` | `list_dir` | List directory contents |
| `ExecTool` | `exec` | Shell command execution |
| `WebSearchTool` | `web_search` | Brave Search API |
| `WebFetchTool` | `web_fetch` | Fetch+readability URL content |
| `MessageTool` | `message` | Send message to channel |
| `SpawnTool` | `spawn` | Spawn a background subagent |
| `CronTool` | `cron` | Schedule agent tasks |
| `MCPToolWrapper` | `mcp_{server}_{tool}` | Wrapped MCP server tools |

**Workspace restriction** (`restrict_to_workspace`):
- When `true`, `ReadFileTool`, `WriteFileTool`, `EditFileTool`, `ListDirTool` enforce `allowed_dir = workspace`
- `ExecTool` also enforces this via working directory + path validation

**`MessageTool` turn tracking**:
- `start_turn()` resets `_sent_in_turn = False`
- When the tool's `execute()` is called, sets `_sent_in_turn = True`
- `AgentLoop` checks: if `_sent_in_turn`, suppresses the final `OutboundMessage` (avoids double-sending)

**Subagent tools**: `spawn` and `message` tools are NOT registered for subagents (they have their own `ToolRegistry` without these).

---

### 4.8 Session Manager (`nanobot/session/manager.py`)

**Per-channel conversation persistence** using JSONL files.

**Session key**: `f"{channel}:{chat_id}"` — e.g. `"telegram:12345678"`, `"cli:direct"`

**File format** (`workspace/sessions/<safe_key>.jsonl`):
```jsonl
{"_type": "metadata", "key": "...", "created_at": "...", "updated_at": "...", "last_consolidated": 0}
{"role": "user",      "content": "Hello", "timestamp": "2026-02-21T10:00:00"}
{"role": "assistant", "content": "Hi!", "timestamp": "2026-02-21T10:00:01", "tools_used": ["web_search"]}
```

**In-memory cache**: `SessionManager._cache` — loaded once, written on every `save()`.

**Legacy migration**: Automatically migrates sessions from `~/.nanobot/sessions/` (old global path) to `workspace/sessions/` (new per-workspace path).

**`get_history()` output format** (LLM-ready):
```python
[
  {"role": "user",      "content": "..."},
  {"role": "assistant", "content": "...", "tool_calls": [...]},
  {"role": "tool",      "content": "...", "tool_call_id": "...", "name": "..."},
  ...
]
```
Tool call metadata (`tool_calls`, `tool_call_id`, `name`) is preserved for multi-step conversations.

---

### 4.9 Provider Layer (`nanobot/providers/`)

**Abstract interface** (`LLMProvider`):
```python
async chat(messages, tools, model, max_tokens, temperature) → LLMResponse
get_default_model() → str
```

**`LLMResponse`**:
```python
content: str|None           # Text response
tool_calls: list[ToolCallRequest]
finish_reason: str
usage: dict                 # prompt/completion/total tokens
reasoning_content: str|None # DeepSeek-R1, Kimi thinking content
```

**Provider implementations**:

| Class | When used |
|-------|-----------|
| `LiteLLMProvider` | Default — all standard providers via LiteLLM |
| `CustomProvider` | When `provider_name == "custom"` — direct OpenAI-compatible |
| `OpenAICodexProvider` | When `provider_name == "openai_codex"` — OAuth-based |

**Provider Registry** (`registry.py` — the `PROVIDERS` tuple):

The registry is the **single source of truth** for all provider metadata. Adding a new provider requires only two steps:
1. Add a `ProviderSpec` entry to `PROVIDERS` in `registry.py`
2. Add a field to `ProvidersConfig` in `config/schema.py`

**`ProviderSpec` fields** (most important):
```python
name              # Config field name, e.g. "dashscope"
keywords          # Model-name keywords for auto-matching, e.g. ("qwen", "dashscope")
env_key           # LiteLLM env var, e.g. "DASHSCOPE_API_KEY"
litellm_prefix    # Auto-prefix: "qwen-max" → "dashscope/qwen-max"
skip_prefixes     # Don't double-prefix, e.g. ("dashscope/",)
is_gateway        # True if can route any model (OpenRouter, AiHubMix)
is_local          # True if local deployment (vLLM)
is_oauth          # True if OAuth-based (Codex, Copilot)
is_direct         # True if bypasses LiteLLM (Custom provider)
supports_prompt_caching  # True for Anthropic/OpenRouter (enables cache_control)
model_overrides   # Per-model parameter overrides e.g. kimi temperature=1.0
```

**Provider matching priority** (in `Config._match_provider`):
1. Explicit provider prefix in model name (e.g., `github-copilot/gpt-4o`)
2. Keyword match (in registry order)
3. Fallback: first gateway with API key, then first provider with API key

**Prompt caching**: When `supports_prompt_caching=True`, `LiteLLMProvider` automatically injects `cache_control: {type: "ephemeral"}` on the last system message block and the last tool definition.

---

### 4.10 Channel Layer (`nanobot/channels/`)

**Abstract interface** (`BaseChannel`):
```python
name: str                              # e.g. "telegram"
async start() → None                   # Long-running: connect + listen
async stop()  → None                   # Cleanup
async send(msg: OutboundMessage) → None
```

**Allow-list checking** (`BaseChannel.is_allowed`):
- `config.allow_from == []` → allow everyone
- Otherwise, sender must be in the list
- Supports compound IDs with `|` separator

**Message ingestion** (`_handle_message`):
```python
# After allow-list check:
msg = InboundMessage(channel, sender_id, chat_id, content, media, metadata)
await bus.publish_inbound(msg)
```

**Channel implementations**:

| Channel | Transport | Library |
|---------|-----------|---------|
| Telegram | Long polling / Webhook | python-telegram-bot |
| Discord | WebSocket (Gateway v10) | Raw websockets |
| WhatsApp | WebSocket to Node.js bridge | websockets |
| Feishu | WebSocket (long connection) | lark-oapi |
| Mochat | Socket.IO | python-socketio |
| DingTalk | Stream mode | dingtalk-stream |
| Slack | Socket Mode | slack-sdk |
| Email | IMAP poll + SMTP reply | imaplib/smtplib (stdlib) |
| QQ | WebSocket | qq-botpy |

**`ChannelManager`**:
- Reads config, conditionally instantiates channels via lazy imports
- `start_all()`: starts all channels + outbound dispatcher as asyncio tasks
- Outbound dispatcher loop: `bus.consume_outbound()` → route to correct channel's `send()`

---

### 4.11 Cron Service (`nanobot/cron/service.py`)

**Purpose**: Schedule agent tasks to run on timers, without a separate process.

**Job types**:
| `kind` | Description |
|--------|-------------|
| `at` | One-shot at a specific millisecond timestamp |
| `every` | Repeat every N milliseconds |
| `cron` | Standard cron expression (uses `croniter` + `zoneinfo`) |

**Persistence**: `~/.nanobot/data/cron/jobs.json` (or `get_data_dir()`)

**Job fields** (`CronJob`):
```python
id              # 8-char UUID prefix
name            # Human label
enabled         # Active flag
schedule        # CronSchedule (kind, at_ms, every_ms, expr, tz)
payload         # CronPayload (kind="agent_turn", message, deliver, channel, to)
state           # CronJobState (next_run_at_ms, last_run_at_ms, last_status, last_error)
delete_after_run # True → remove after one-shot execution
```

**Timer mechanism**: Single `asyncio.Task` (not a periodic loop). After each tick, `_arm_timer()` computes the next wake time and schedules a new task. This is efficient — O(1) sleeping, no polling.

**Job execution callback** (`on_job`):
```python
async def on_cron_job(job: CronJob) -> str | None:
    response = await agent.process_direct(job.payload.message, ...)
    if job.payload.deliver:
        await bus.publish_outbound(OutboundMessage(...))
    return response
```

---

### 4.12 Heartbeat Service (`nanobot/heartbeat/service.py`)

**Purpose**: Proactive agent wake-up every 30 minutes to check `HEARTBEAT.md` for user-defined tasks.

**Flow**:
1. Every `interval_s` (default: 1800 seconds), read `workspace/HEARTBEAT.md`
2. If file is empty / only headers / only checkboxes → skip (no LLM call)
3. Otherwise: call `on_heartbeat(HEARTBEAT_PROMPT)` which runs `agent.process_direct()`
4. If agent response contains `HEARTBEAT_OK` → logged as "nothing to do"

**`HEARTBEAT_PROMPT`**: Instructs agent to read HEARTBEAT.md and follow instructions.

**Use case**: User puts recurring tasks or monitoring instructions in HEARTBEAT.md; agent executes them periodically without user triggering.

---

### 4.13 MCP Integration (`nanobot/agent/tools/mcp.py`)

**Model Context Protocol** — connects external tool servers.

**Transport modes**:
| Mode | Config | Implementation |
|------|--------|----------------|
| Stdio | `command` + `args` + `env` | `mcp.client.stdio.stdio_client` |
| HTTP | `url` + `headers` | `mcp.client.streamable_http.streamable_http_client` |

**Lifecycle**:
- Initialized lazily on first message (after `AgentLoop.run()`)
- All sessions managed via `AsyncExitStack` (closed on `close_mcp()`)
- One-time retry if connection fails: next message re-attempts

**Tool naming**: `mcp_{server_name}_{tool_name}` — avoids collisions with built-in tools.

**`MCPToolWrapper`**: Wraps MCP `tool_def` into nanobot's `Tool` ABC. `execute()` calls `session.call_tool()` with kwargs and returns concatenated `TextContent` parts.

---

### 4.14 Configuration System (`nanobot/config/`)

**Config file**: `~/.nanobot/config.json` (camelCase JSON, auto-migrated)

**Schema** (Pydantic `Config` root model):
```
Config
├── agents: AgentsConfig
│   └── defaults: AgentDefaults
│       ├── workspace: str          (~/.nanobot/workspace)
│       ├── model: str              (anthropic/claude-opus-4-5)
│       ├── max_tokens: int         (8192)
│       ├── temperature: float      (0.7)
│       ├── max_tool_iterations: int (20)
│       └── memory_window: int      (50)
│
├── channels: ChannelsConfig
│   ├── telegram: TelegramConfig
│   ├── discord: DiscordConfig
│   ├── whatsapp: WhatsAppConfig
│   ├── feishu: FeishuConfig
│   ├── mochat: MochatConfig
│   ├── dingtalk: DingTalkConfig
│   ├── slack: SlackConfig
│   ├── email: EmailConfig
│   └── qq: QQConfig
│
├── providers: ProvidersConfig
│   └── {provider_name}: ProviderConfig(api_key, api_base, extra_headers)
│
├── gateway: GatewayConfig
│   ├── host: str (0.0.0.0)
│   └── port: int (18790)
│
└── tools: ToolsConfig
    ├── web.search.api_key: str    (Brave)
    ├── exec.timeout: int          (60)
    ├── restrict_to_workspace: bool
    └── mcp_servers: dict[str, MCPServerConfig]
```

**Key method**: `Config._match_provider(model)` — matches API key + provider spec by keyword priority.

**Env variable override**: `NANOBOT_*` prefix, nested with `__`. E.g., `NANOBOT_AGENTS__DEFAULTS__MODEL=gpt-4o`.

**Config migration**: `_migrate_config()` handles schema changes between versions (currently: `tools.exec.restrictToWorkspace` → `tools.restrictToWorkspace`).

---

### 4.15 WhatsApp Bridge (`bridge/`)

**Purpose**: WhatsApp has no official bot API. The bridge is a Node.js/TypeScript service using `whatsapp-web.js` that:
1. Authenticates via QR code scan
2. Receives WhatsApp messages
3. Forwards them to the Python agent via WebSocket
4. Receives responses from Python and sends them back to WhatsApp

**Protocol**: WebSocket at `ws://localhost:3001` (default). Optional `bridge_token` for auth.

**Setup**: Bundled in the Python package. `nanobot channels login` copies bridge source to `~/.nanobot/bridge/`, runs `npm install && npm run build`, then starts it.

---

## 5. Data Flow Diagrams

### 5.1 Inbound Message Flow (Gateway Mode)

```
External Platform
       │
       │  (platform-specific: HTTP webhook / WebSocket / IMAP poll)
       ▼
Channel (e.g., TelegramChannel.start())
       │
       │  is_allowed(sender_id)?
       │       NO → log warning, drop
       │       YES ↓
       ▼
InboundMessage created
       │
       ▼
bus.publish_inbound(msg)
       │
       ▼
asyncio.Queue[InboundMessage]
       │
       ▼
AgentLoop.run() → bus.consume_inbound()
       │
       ▼
AgentLoop._process_message(msg)
       │
       ├─ /new or /help → OutboundMessage → bus.publish_outbound
       │
       ├─ session = sessions.get_or_create(session_key)
       │
       ├─ if len(session.messages) > window:
       │     asyncio.create_task(_consolidate_memory)   [background]
       │
       ├─ _set_tool_context(channel, chat_id, message_id)
       │
       ├─ initial_messages = context.build_messages(...)
       │
       ▼
AgentLoop._run_agent_loop(initial_messages)
       │
       │  ┌─────────────────────────────────────────────┐
       │  │  LLM loop (max_iterations=20)               │
       │  │                                              │
       │  │  provider.chat(messages, tools, model, ...)  │
       │  │         │                                    │
       │  │    response.has_tool_calls?                  │
       │  │         │                                    │
       │  │    YES──┤                                    │
       │  │         │  publish _progress message         │
       │  │         │  append assistant msg              │
       │  │         │  execute each tool → append result │
       │  │         │  repeat loop                       │
       │  │         │                                    │
       │  │    NO───┤                                    │
       │  │         │  final_content = response.content  │
       │  │         │  break                             │
       │  └─────────────────────────────────────────────┘
       │
       ├─ session.add_message("user", content)
       ├─ session.add_message("assistant", final_content)
       ├─ sessions.save(session)
       │
       ▼
OutboundMessage(channel, chat_id, final_content)
       │
       ▼
bus.publish_outbound(msg)
       │
       ▼
ChannelManager._dispatch_outbound()
       │
       ▼
channel.send(msg)
       │
       ▼
External Platform (reply sent)
```

### 5.2 Subagent Flow

```
AgentLoop detects spawn tool call
       │
       ▼
SpawnTool.execute(task, label)
       │
       ▼
SubagentManager.spawn(task, label, channel, chat_id)
       │
       ├─ asyncio.create_task(_run_subagent(...))  [runs in background]
       │
       ▼ [immediately]
"Subagent started..." → returned to main loop → user sees response

       [background task runs independently]
       │
       ▼
_run_subagent(task_id, task, label, origin)
       │
       ├─ Build isolated ToolRegistry (no message/spawn tools)
       ├─ Build subagent system prompt
       │
       ├─ LLM loop (max 15 iterations, same tools: fs/shell/web)
       │
       ▼
final_result produced
       │
       ▼
_announce_result → bus.publish_inbound(InboundMessage(
    channel="system",
    sender_id="subagent",
    chat_id=f"{origin_channel}:{origin_chat_id}",
    content=f"[Subagent '{label}' completed]\n...\n{result}"
))
       │
       ▼
AgentLoop._process_message sees channel=="system"
       │
       ▼
_process_system_message(msg)
       │
       ├─ Parses origin from chat_id
       ├─ Loads original session
       ├─ Runs LLM to summarize result
       │
       ▼
User receives natural-language summary of subagent's work
```

### 5.3 Cron Job Flow

```
CronService starts → _arm_timer()
       │
       │  asyncio.sleep(delay_to_next_job)
       │
       ▼
_on_timer() → find due jobs
       │
       ▼
_execute_job(job)
       │
       ▼
on_job(job) callback (set in cli/commands.py gateway())
       │
       ▼
agent.process_direct(job.payload.message, session_key=f"cron:{job.id}")
       │
       ├─ [optional] if job.payload.deliver:
       │     bus.publish_outbound → channel sends to user
       │
       ▼
job.state updated → _save_store() → _arm_timer() for next
```

### 5.4 Memory Consolidation Flow

```
[Triggered when: len(session.messages) > memory_window]

asyncio.create_task(_consolidate_memory(session))
       │
       ├─ old_messages = session.messages[last_consolidated:-keep_count]
       │
       ├─ Build conversation transcript string
       │
       ├─ current_memory = memory.read_long_term()
       │
       ├─ provider.chat([consolidation prompt]) → JSON response
       │
       ├─ memory.append_history(entry)
       ├─ memory.write_long_term(update)
       │
       ▼
session.last_consolidated = len(session.messages) - keep_count
```

---

## 6. Key Design Patterns

### 6.1 Registry Pattern
Used for both **providers** and **tools**:
- `PROVIDERS` tuple in `registry.py` — single source of truth, drives all matching + display
- `ToolRegistry` — dynamic dict, tools registered at startup (+ MCP tools on first connect)

### 6.2 Abstract Base Classes (ABCs)
Every extension point has a clean ABC:
- `LLMProvider` → `chat()`, `get_default_model()`
- `BaseChannel` → `start()`, `stop()`, `send()`
- `Tool` → `name`, `description`, `parameters`, `execute()`

### 6.3 Async Queue Decoupling
The message bus pattern completely decouples channels from the agent. Channels only need `bus.publish_inbound()`. The agent only calls `bus.consume_inbound()`. This enables:
- Adding new channels without touching agent code
- Testing agent logic independently of any channel

### 6.4 Callback Injection
Cron and Heartbeat services receive their execution callbacks at construction time:
```python
cron.on_job = async def on_cron_job(job) → str
heartbeat.on_heartbeat = async def on_heartbeat(prompt) → str
```
This keeps the services decoupled from the agent.

### 6.5 Progressive Skill Loading
Skills are not all loaded into the system prompt. Only `always=true` skills are inline; others appear as an XML summary. The agent reads SKILL.md files on demand. This keeps prompt size manageable.

### 6.6 Lazy MCP Connection
MCP servers connect on first use, not at startup. Errors retry on next message. This prevents startup failures from unresponsive MCP servers.

### 6.7 Workspace-Relative Paths
All persistent state lives under the workspace:
```
workspace/
  memory/MEMORY.md      ← long-term facts
  memory/HISTORY.md     ← history log
  sessions/*.jsonl      ← conversation sessions
  skills/*/SKILL.md     ← user-defined skills
  AGENTS.md             ← behavior instructions
  HEARTBEAT.md          ← periodic tasks
```
The workspace is configurable — multiple workspaces = multiple agent personalities.

---

## 7. Workspace Layout at Runtime

```
~/.nanobot/
├── config.json                     # Global configuration
├── history/
│   └── cli_history                 # prompt_toolkit CLI input history
└── data/
    └── cron/
        └── jobs.json               # Cron job persistence

~/.nanobot/workspace/               # Default workspace (configurable)
├── AGENTS.md                       # Agent behavior (editable by user)
├── SOUL.md                         # Personality (editable by user)
├── USER.md                         # User profile (editable by user)
├── TOOLS.md                        # Tool instructions
├── HEARTBEAT.md                    # Periodic task list
├── IDENTITY.md                     # (optional) Custom identity
├── memory/
│   ├── MEMORY.md                   # LLM-maintained long-term facts
│   └── HISTORY.md                  # Append-only timestamped history
├── sessions/
│   ├── telegram_12345678.jsonl     # Per-channel conversation sessions
│   ├── cli_direct.jsonl
│   └── cron_abc12345.jsonl
└── skills/
    └── <custom-skill>/
        └── SKILL.md                # User-defined skills
```

---

## 8. Dependency Graph

```
cli/commands.py
    ├── config/loader.py
    │       └── config/schema.py (Pydantic models)
    ├── bus/queue.py
    │       └── bus/events.py
    ├── agent/loop.py
    │       ├── agent/context.py
    │       │       ├── agent/memory.py
    │       │       └── agent/skills.py
    │       ├── agent/subagent.py
    │       │       ├── agent/tools/registry.py
    │       │       └── agent/tools/{filesystem,shell,web}.py
    │       ├── agent/tools/registry.py
    │       │       └── agent/tools/base.py
    │       ├── agent/tools/{filesystem,shell,web,message,spawn,cron,mcp}.py
    │       ├── session/manager.py
    │       └── providers/base.py
    ├── channels/manager.py
    │       ├── channels/base.py
    │       └── channels/{telegram,discord,whatsapp,...}.py
    ├── providers/{litellm,custom,openai_codex}_provider.py
    │       ├── providers/base.py
    │       └── providers/registry.py
    ├── cron/service.py
    │       └── cron/types.py
    └── heartbeat/service.py

External dependencies (pyproject.toml):
    litellm          → multi-provider LLM routing
    pydantic         → config schema
    typer + rich     → CLI
    websockets       → Discord, WhatsApp
    python-telegram-bot → Telegram
    lark-oapi        → Feishu
    dingtalk-stream  → DingTalk
    slack-sdk        → Slack
    qq-botpy         → QQ
    python-socketio  → Mochat
    croniter         → cron expression parsing
    httpx            → HTTP client (MCP HTTP transport)
    mcp              → MCP SDK
    json-repair      → robust JSON parsing
    loguru           → logging
    prompt-toolkit   → CLI input
    readability-lxml → web content extraction
    oauth-cli-kit    → OAuth flow (Codex/Copilot)
```

---

## 9. Extension Points for Future Development

### 9.1 Adding a New Channel

1. Create `nanobot/channels/<name>.py` implementing `BaseChannel`:
   ```python
   class MyChannel(BaseChannel):
       name = "mychannel"
       async def start(self): ...  # connect + listen loop
       async def stop(self): ...
       async def send(self, msg: OutboundMessage): ...
   ```
2. Add config to `nanobot/config/schema.py`:
   ```python
   class MyChannelConfig(Base):
       enabled: bool = False
       token: str = ""
       allow_from: list[str] = Field(default_factory=list)

   class ChannelsConfig(Base):
       ...
       mychannel: MyChannelConfig = Field(default_factory=MyChannelConfig)
   ```
3. Register in `nanobot/channels/manager.py` `_init_channels()`:
   ```python
   if self.config.channels.mychannel.enabled:
       from nanobot.channels.mychannel import MyChannel
       self.channels["mychannel"] = MyChannel(self.config.channels.mychannel, self.bus)
   ```

**Does NOT require**: Changes to AgentLoop, MessageBus, or ContextBuilder.

### 9.2 Adding a New LLM Provider

1. Add `ProviderSpec` to `PROVIDERS` in `nanobot/providers/registry.py`
2. Add field to `ProvidersConfig` in `nanobot/config/schema.py`

**No other changes needed** — env setup, model prefixing, status display, and fallback matching all derive automatically.

### 9.3 Adding a New Built-in Tool

1. Create `nanobot/agent/tools/<name>.py` implementing `Tool`:
   ```python
   class MyTool(Tool):
       @property
       def name(self): return "my_tool"
       @property
       def description(self): return "Does something useful"
       @property
       def parameters(self):
           return {"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]}
       async def execute(self, arg: str) -> str:
           return f"Result: {arg}"
   ```
2. Register in `AgentLoop._register_default_tools()`:
   ```python
   self.tools.register(MyTool())
   ```

### 9.4 Adding a New Skill

Create `workspace/skills/<name>/SKILL.md` (user-side) or `nanobot/skills/<name>/SKILL.md` (built-in):
```markdown
---
name: "myskill"
description: "What this skill does"
metadata: '{"nanobot": {"requires": {"bins": ["sometool"]}, "always": false}}'
---

# My Skill

Instructions for the agent...
```

### 9.5 Adding a New Workspace Bootstrap File

Add the filename to `ContextBuilder.BOOTSTRAP_FILES` in `context.py`:
```python
BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md", "MYFILE.md"]
```
Create the template in `_create_workspace_templates()` in `cli/commands.py`.

### 9.6 Customizing Agent Identity

Create `workspace/IDENTITY.md` — it is loaded as a bootstrap file in the system prompt. This is the **recommended way** to customize the agent personality in forks without modifying `context.py`.

### 9.7 Adding a Custom LLM Provider Class

For providers not covered by LiteLLM, subclass `LLMProvider`:
```python
class MyProvider(LLMProvider):
    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7) -> LLMResponse:
        # Call your API
        return LLMResponse(content="...", tool_calls=[])
    def get_default_model(self) -> str:
        return "my-model"
```
Register in `cli/commands.py` `_make_provider()`.

---

## 10. Fork Rules — What to Change vs. What to Preserve

### PRESERVE — Core Contracts (Do Not Break)

| Component | What to preserve | Why |
|-----------|-----------------|-----|
| `MessageBus` | `InboundMessage`/`OutboundMessage` dataclass fields | All channels + agent depend on these |
| `Tool` ABC | `name`, `description`, `parameters`, `execute` interface | ToolRegistry depends on this |
| `LLMProvider` ABC | `chat()` signature, `LLMResponse` fields | All providers implement this |
| `BaseChannel` ABC | `start()`, `stop()`, `send()` signature | ChannelManager depends on this |
| `Session.get_history()` | Output format (role/content/tool_calls) | LLM expects this format |
| `ProviderSpec` fields | All existing fields | registry.py lookups depend on all fields |
| `Config` schema keys | Existing field names | User configs would break on rename |
| Workspace file names | `MEMORY.md`, `HISTORY.md`, session `*.jsonl` format | Existing user data compatibility |
| `session_key` format | `f"{channel}:{chat_id}"` | Cross-module routing contract |
| `_progress` metadata flag | OutboundMessage metadata key | CLI and channels use this |
| `system` channel | Subagent routing convention | SubagentManager depends on this |

### SAFE TO CHANGE — Fork Extension Areas

| Area | What you can safely add/modify |
|------|-------------------------------|
| `workspace/IDENTITY.md` | Agent personality, no code changes |
| `workspace/AGENTS.md` | Behavior instructions |
| `workspace/SOUL.md` | Personality |
| `nanobot/skills/` | Add new skill directories |
| `ContextBuilder.BOOTSTRAP_FILES` | Add new bootstrap file names |
| `nanobot/channels/` | Add new channel files |
| `nanobot/providers/registry.py` | Add new `ProviderSpec` entries |
| `nanobot/config/schema.py` | Add new config fields (never remove existing) |
| `AgentLoop._register_default_tools()` | Register additional tools |
| `cli/commands.py` commands | Add new CLI subcommands |
| New files/modules | Anything new that doesn't conflict |

### MERGE STRATEGY

When pulling upstream changes:
1. Upstream changes to `bus/events.py`, `agent/tools/base.py`, `providers/base.py`, `channels/base.py` → **review carefully**, may require updating downstream implementations
2. Upstream new channels/providers → **safe to merge**, unlikely to conflict
3. Upstream config schema additions → **safe to merge** (additive)
4. Upstream config schema renames → **requires migration**, update `_migrate_config()`

---

## 11. Adding New Components — Step-by-Step Guides

### Guide A: New Channel (Complete)

```
1. nanobot/config/schema.py
   → Add MyChannelConfig(Base) class
   → Add field to ChannelsConfig

2. nanobot/channels/mychannel.py
   → Implement BaseChannel
   → name = "mychannel"
   → start(): connect, listen loop, call self._handle_message()
   → stop(): disconnect
   → send(msg): deliver OutboundMessage

3. nanobot/channels/manager.py _init_channels()
   → if config.channels.mychannel.enabled:
         from nanobot.channels.mychannel import MyChannel
         self.channels["mychannel"] = MyChannel(...)

4. README.md (optional)
   → Document configuration fields

No changes to: bus/, agent/, providers/, cron/, heartbeat/, session/
```

### Guide B: New Provider (Complete)

```
1. nanobot/providers/registry.py
   → Add ProviderSpec entry to PROVIDERS tuple

2. nanobot/config/schema.py
   → Add field to ProvidersConfig

That's it. The following work automatically:
  - nanobot status display
  - API key env var setup
  - Model name prefixing
  - Provider matching
  - Fallback selection
```

### Guide C: New Tool (Complete)

```
1. nanobot/agent/tools/mytool.py
   → Implement Tool ABC
   → name, description, parameters, execute()

2. nanobot/agent/loop.py _register_default_tools()
   → from nanobot.agent.tools.mytool import MyTool
   → self.tools.register(MyTool(...))

No changes to: registry.py (tools), channels/, providers/
```

### Guide D: New Skill

```
1. nanobot/skills/myskill/SKILL.md
   → Add frontmatter (name, description, metadata)
   → Write agent instructions in Markdown

No code changes needed.
```

---

## 12. Security Architecture

### 12.1 Access Control
- **`allowFrom` lists** on every channel: empty = public, non-empty = whitelist
- `BaseChannel.is_allowed()` checks sender before publishing to bus
- Email has additional `consentGranted` flag before mailbox access

### 12.2 Workspace Sandboxing
- `tools.restrict_to_workspace: true` → all file tools enforce `allowed_dir = workspace`
- `exec` tool working directory is set to workspace
- Shell commands can still access the full filesystem unless the process user is restricted

### 12.3 Command Injection Prevention
- `ExecTool` runs commands via `asyncio.create_subprocess_shell` with a timeout
- No user-supplied input is directly concatenated into shell strings from nanobot's own code

### 12.4 API Key Protection
- Keys stored in `~/.nanobot/config.json` (user-mode file)
- Never logged at INFO level
- WhatsApp `bridge_token` is passed as env var, not in URL

### 12.5 MCP Security
- MCP servers run as separate processes (stdio) or remote endpoints (HTTP)
- Tools are auto-registered with `mcp_{server}_{tool}` namespacing

---

## 13. Testing Structure

```
tests/
├── test_cli_input.py         # CLI input handling (terminal flush, prompt session)
├── test_commands.py          # CLI command smoke tests
├── test_consolidate_offset.py # Memory consolidation offset logic
├── test_cron_commands.py     # Cron CLI commands
├── test_cron_service.py      # CronService unit tests
├── test_docker.sh            # Docker integration test script
├── test_email_channel.py     # Email channel parsing
└── test_tool_validation.py   # Tool parameter validation (Tool.validate_params)
```

**Test framework**: pytest + pytest-asyncio (asyncio_mode = "auto")

**Key test patterns**:
- Mock `bus.publish_inbound` / `bus.publish_outbound` for channel tests
- Mock `LLMProvider.chat()` for agent loop tests
- Use `tmp_path` fixture for workspace isolation

---

## 14. Deployment Architecture

### 14.1 Local Development
```bash
git clone https://github.com/HKUDS/nanobot.git
cd nanobot
pip install -e .
nanobot onboard
nanobot agent -m "Hello"
```

### 14.2 Docker (Single Container)
```dockerfile
FROM python:3.11-slim
# Dockerfile already present in repo
# Config mounted at /root/.nanobot
docker run -v ~/.nanobot:/root/.nanobot nanobot gateway
```

### 14.3 Docker Compose
```yaml
services:
  nanobot-gateway:  # Long-running gateway with all channels
  nanobot-cli:      # One-shot CLI commands
```

### 14.4 Port Exposure
- Port `18790` (default): reserved for potential HTTP gateway (currently unused in production)
- WhatsApp bridge WebSocket: `localhost:3001` (internal only)

### 14.5 Process Model
- **Single process, single event loop**
- Agent loop + all channels + cron + heartbeat run as asyncio tasks within one `asyncio.gather()`
- No multiprocessing; concurrency is cooperative (awaitable I/O only)
- MCP server processes are children spawned via stdio transport

### 14.6 Scaling Considerations
- Not designed for horizontal scaling (in-memory bus, single-process)
- For multi-user scale: run separate nanobot instances per workspace
- The message bus could be replaced with Redis/NATS (change `queue.py` implementation only)

---

## Appendix: File-to-Concept Quick Reference

| Concept | File |
|---------|------|
| Agent processing loop | `nanobot/agent/loop.py` |
| System prompt assembly | `nanobot/agent/context.py` |
| Long-term memory | `nanobot/agent/memory.py` |
| Skill loading | `nanobot/agent/skills.py` |
| Background tasks | `nanobot/agent/subagent.py` |
| Tool abstraction | `nanobot/agent/tools/base.py` |
| Tool management | `nanobot/agent/tools/registry.py` |
| MCP client | `nanobot/agent/tools/mcp.py` |
| Message queue | `nanobot/bus/queue.py` |
| Message types | `nanobot/bus/events.py` |
| Channel contract | `nanobot/channels/base.py` |
| Channel orchestration | `nanobot/channels/manager.py` |
| Config schema | `nanobot/config/schema.py` |
| Config loading | `nanobot/config/loader.py` |
| Scheduled tasks | `nanobot/cron/service.py` |
| Periodic wake-up | `nanobot/heartbeat/service.py` |
| LLM contract | `nanobot/providers/base.py` |
| Provider registry | `nanobot/providers/registry.py` |
| LiteLLM adapter | `nanobot/providers/litellm_provider.py` |
| Session persistence | `nanobot/session/manager.py` |
| CLI entry point | `nanobot/cli/commands.py` |
| WhatsApp bridge | `bridge/src/` |

---

*Document generated: 2026-02-21. Based on nanobot v0.1.4 (fork of HKUDS/nanobot). Review and update after each significant upstream merge.*
