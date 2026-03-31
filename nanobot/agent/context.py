"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.skills import SkillsLoader
from nanobot.utils.helpers import detect_image_mime


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.skills = SkillsLoader(workspace)
        self._bootstrap_cache: str | None = None
        self._bootstrap_mtime: float = 0.0

    def _bootstrap_mtime_now(self) -> float:
        """Max mtime of bootstrap files for cache invalidation."""
        m = 0.0
        for name in self.BOOTSTRAP_FILES:
            p = self.workspace / name
            if p.exists():
                try:
                    m = max(m, p.stat().st_mtime)
                except OSError:
                    pass
        return m

    def build_system_prompt(self, memory_context: str = "") -> str:
        """Build the system prompt as a single string (backward compat).

        memory_context is pre-fetched by the caller (awaited from the memory store)
        and injected here so this method can remain synchronous.
        """
        content = self._build_system_content(memory_context)
        if isinstance(content, str):
            return content
        return "\n".join(block["text"] for block in content)

    def _build_static_system_prompt(self) -> str:
        """Build the STATIC portion of the system prompt (identity + bootstrap + skills).

        This content rarely changes between requests and is the primary
        beneficiary of prompt caching — it should always be the first content
        block sent to the provider.
        """
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    _WEBUI_JSON_FORMAT = """\
## Response Format (WebUI)

You are responding in the WebUI channel. ALL responses must be a single JSON \
object — no surrounding text, no markdown fences.

After a MCP tool call that returned data — output ONLY the meta object.
The MCP payload is forwarded to the UI automatically:
{"meta": {"next_actions": ["Download as CSV", "Filter to India"]}}

Conversational response (no MCP data):
{"text": "your response in markdown", "meta": {"next_actions": [...]}}

Error:
{"error": "what went wrong", "meta": {"next_actions": [...]}}

Rules:
- Always include meta.next_actions — 2–4 short specific follow-up suggestions \
based on what just happened (e.g. "Fetch contacts from \\"CloudThat\\"", \
"Export as CSV", "Filter to companies in India").
- After a MCP data call output ONLY {"meta": {...}} — nothing else.
- Raw JSON only. No markdown fences, no prose before or after."""

    def _build_system_content(
        self,
        memory_context: str = "",
        ephemeral_skill: str | None = None,
        company_dna: dict[str, Any] | None = None,
        channel: str | None = None,
    ) -> str | list[dict[str, Any]]:
        """Build system content as cache-friendly blocks.

        Static content (identity + bootstrap + skills) goes first as a single
        block — this is the cacheable prefix that the provider can reuse across
        requests.  Dynamic content (company DNA + memory + ephemeral skill) goes
        last so changes don't invalidate the cached prefix.

        Returns a plain string when there's no dynamic content, or a list of
        content blocks ``[{"type": "text", "text": ...}, ...]`` when memory
        context, company DNA, or ephemeral skill is present.
        """
        static = self._build_static_system_prompt()
        dna_block = self._render_company_dna(company_dna) if company_dna else ""
        webui_format = self._WEBUI_JSON_FORMAT if channel == "webui" else ""
        if not memory_context and not ephemeral_skill and not dna_block and not webui_format:
            return static
        blocks: list[dict[str, Any]] = [{"type": "text", "text": static}]
        if dna_block:
            blocks.append({"type": "text", "text": f"\n\n---\n\n{dna_block}"})
        if memory_context:
            blocks.append({"type": "text", "text": f"\n\n---\n\n# Memory\n\n{memory_context}"})
        if ephemeral_skill:
            blocks.append({"type": "text", "text": f"\n\n---\n\n# Active Skill Context\n\n{ephemeral_skill}"})
        if webui_format:
            blocks.append({"type": "text", "text": f"\n\n---\n\n{webui_format}"})
        return blocks

    @staticmethod
    def _render_company_dna(dna: dict[str, Any]) -> str:
        """Render the company DNA dict as a GTM-focused system prompt section.

        Handles two formats:
        - Rich format: nested under company_snapshot / company_icp / contact_icp /
          competitive_landscape / offerings_and_value_proposition / recommended_signals
        - Simple format: flat keys (company_name, industry, icp, value_propositions, ...)
        """
        snap: dict = dna.get("company_snapshot", {})

        # ── Identity ──────────────────────────────────────────────────────────
        name = snap.get("brand_name") or snap.get("legal_name") or dna.get("company_name", "your company")
        industry = (snap.get("industry") or {}).get("primary") or dna.get("industry", "")
        website = snap.get("domain") or dna.get("website", "")
        desc_obj = snap.get("description") or dna.get("description", "")
        description = desc_obj.get("short") if isinstance(desc_obj, dict) else str(desc_obj)
        revenue = (snap.get("revenue") or {}).get("value", "")
        employee_range = (snap.get("employee_count") or {}).get("range", "")

        # ── Company ICP ───────────────────────────────────────────────────────
        company_icp_list: list = dna.get("company_icp", [])
        flat_icp: dict = dna.get("icp", {})
        cicp = company_icp_list[0] if company_icp_list else {}

        target_industries = [
            i["name"] for i in cicp.get("target_industries", [])
            if i.get("tier") == "primary"
        ] or flat_icp.get("industries", [])

        size_sweet_spot = (cicp.get("company_size") or {}).get("sweet_spot") or ""
        size_segments = (cicp.get("company_size") or {}).get("segments") or flat_icp.get("company_sizes", [])

        geographies = [
            g["region"] for g in cicp.get("geography", [])
        ] or flat_icp.get("geographies", [])

        # ── Contact ICP ───────────────────────────────────────────────────────
        contact_icp_list: list = dna.get("contact_icp", [])
        ccip = contact_icp_list[0] if contact_icp_list else {}
        primary_titles = [
            t["title"] for t in ccip.get("target_titles", [])
            if t.get("priority") == "primary"
        ] or flat_icp.get("personas", [])
        seniority_sweet_spot = ccip.get("seniority_sweet_spot", "")

        # ── Value propositions / differentiators ──────────────────────────────
        ovp: dict = dna.get("offerings_and_value_proposition", {})
        differentiators = [d["statement"] for d in ovp.get("key_differentiators", [])]
        flat_vp: list = dna.get("value_propositions", [])
        value_props = differentiators or flat_vp

        # ── Competitors ───────────────────────────────────────────────────────
        comp_landscape: list = dna.get("competitive_landscape", [])
        competitors = [
            f"{c['company_name']} ({c.get('key_difference', '')})"
            for c in comp_landscape if c.get("overlap_type") == "direct"
        ] or dna.get("competitors", [])

        # ── GTM signals ───────────────────────────────────────────────────────
        signals: dict = dna.get("recommended_signals", {})
        critical_signals = [
            s["signal_name"] for s in signals.get("industry_specific", [])
            if s.get("priority") == "critical"
        ] + [
            s["signal_name"] for s in signals.get("standard_gtm", [])
            if s.get("priority") == "high"
        ]

        # ── Products ──────────────────────────────────────────────────────────
        products = snap.get("products_and_services", [])

        # ── Render ────────────────────────────────────────────────────────────
        lines: list[str] = []
        header = f"# Company Context\nYou are acting as a dedicated GTM Engineer for **{name}**"
        if industry:
            header += f" — {industry}"
        header += "."
        if website:
            header += f" ({website})"
        lines.append(header)

        if description:
            lines.append(f"\n**About**: {description}")

        if revenue or employee_range:
            meta_parts = []
            if revenue:
                meta_parts.append(f"Revenue: {revenue}")
            if employee_range:
                meta_parts.append(f"Employees: {employee_range}")
            lines.append(f"**Scale**: {' | '.join(meta_parts)}")

        if products:
            lines.append("\n**Products**:")
            for p in products[:5]:
                lines.append(f"- **{p['name']}**: {p.get('one_liner', '')}")

        lines.append("\n**Ideal Customer Profile (ICP)**:")
        if target_industries:
            lines.append(f"- Industries: {', '.join(target_industries)}")
        if size_sweet_spot:
            lines.append(f"- Company size sweet-spot: {size_sweet_spot} employees")
        elif size_segments:
            lines.append(f"- Company sizes: {', '.join(size_segments)}")
        if geographies:
            lines.append(f"- Geographies: {', '.join(geographies)}")
        if primary_titles:
            lines.append(f"- Primary personas: {', '.join(primary_titles)}")
        if seniority_sweet_spot:
            lines.append(f"- Seniority sweet-spot: {seniority_sweet_spot}")

        if value_props:
            lines.append("\n**Key Differentiators**:")
            for vp in value_props[:5]:
                lines.append(f"- {vp}")

        if competitors:
            lines.append("\n**Direct Competitors**:")
            for c in competitors[:4]:
                lines.append(f"- {c}")

        if critical_signals:
            lines.append("\n**Highest-Priority Buying Signals to Watch**:")
            for s in critical_signals[:5]:
                lines.append(f"- {s}")

        lines.append(
            "\nWhen discovering contacts, filtering segments, or analysing data — "
            "always apply this ICP. Never ask the user to re-explain their target market. "
            "If a query is ambiguous, default to this profile."
        )
        return "\n".join(lines)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        platform_policy = ""
        if system == "Windows":
            platform_policy = """## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or file tools when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled.
"""
        else:
            platform_policy = """## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
"""
        memory_section = (
            "- Long-term memory: Managed automatically via PostgreSQL + pgvector. "
            "Semantic recall is built-in. Do NOT write to MEMORY.md directly."
        )

        return f"""# nanobot 🐈

You are nanobot, a helpful AI assistant.

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
{memory_section}
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

{platform_policy}

## nanobot Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel.

## Data & Enrichment Protocol

### CODE MODE — 2-Step Tool Protocol
When the user asks to enrich, search, or fetch contacts/companies:
1. **Search first**: Call the MCP `search` tool to find the right API endpoint for the request.
2. **Execute second**: Call the MCP `execute` tool with the endpoint found in step 1.
Never call `execute` without first calling `search` unless you already know the exact endpoint from this session's memory.

### Resultset Management
- Every API result is stored with a `resultset_ref` — a unique ID for that dataset.
- Use `list_resultsets` to see all datasets collected in this session.
- When a user asks for a subset (e.g., "filter to India"), check `list_resultsets` first.
  If a relevant resultset exists, reference its `resultset_ref` in your next MCP call instead of re-fetching.

### Data Receipt Protocol
After any MCP data result (contact_company kind):
1. Output a brief summary: "Found X contacts/companies."
2. Show a preview table (first 5 rows, key columns).
3. Output `[Preview](#preview-last)` sentinel on its own line.
4. Show action buttons: 📥 Download CSV | 📤 Push to Segment | 🔗 Push to Webhook"""

    @staticmethod
    def _build_runtime_context(
        channel: str | None,
        chat_id: str | None,
        active_resultset_id: str | None = None,
        session_datasets: list[dict] | None = None,
    ) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        if session_datasets and len(session_datasets) > 1:
            # Multiple datasets: show full list so LLM picks the right one by name/context.
            lines.append("Available Datasets (pick the correct resultset_id based on context):")
            for i, ds in enumerate(session_datasets, 1):
                rs_id = ds.get("resultset_id", "?")
                label = ds.get("label") or "Dataset"
                rows = ds.get("row_count") or 0
                rows_str = f" — {rows} rows" if rows else ""
                marker = " ← latest" if i == len(session_datasets) else ""
                lines.append(f"  {i}. {label}{rows_str} [ID: {rs_id}]{marker}")
        elif active_resultset_id:
            lines.append(f"Active Resultset ID: {active_resultset_id}")
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace (cached by mtime)."""
        current = self._bootstrap_mtime_now()
        if current > self._bootstrap_mtime or self._bootstrap_cache is None:
            self._bootstrap_mtime = current
            parts = []
            for filename in self.BOOTSTRAP_FILES:
                file_path = self.workspace / filename
                if file_path.exists():
                    content = file_path.read_text(encoding="utf-8")
                    parts.append(f"## {filename}\n\n{content}")
            self._bootstrap_cache = "\n\n".join(parts) if parts else ""
        return self._bootstrap_cache

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        memory_context: str = "",
        synthetic_turns: list[dict[str, Any]] | None = None,
        ephemeral_skill_name: str | None = None,
        active_resultset_id: str | None = None,
        session_datasets: list[dict] | None = None,
        company_dna: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call.

        System content is structured as [static_block, dynamic_block] when
        memory or an ephemeral skill is present, enabling providers with prompt
        caching to cache the large static prefix independently of per-turn context.

        synthetic_turns: pre-built assistant + tool messages injected after the
          user message (used by slash-command synthetic context injection so the
          LLM sees a completed tool call before generating its response).
        ephemeral_skill_name: name of a skill to load and inject for this turn only.
        active_resultset_id: if set, appended to the runtime context block so the
          LLM always knows the current dataset without searching history.
        session_datasets: if multiple datasets exist, shows the full list with labels
          so the LLM can pick the right one by context instead of blindly using the last.
        """
        ephemeral_skill: str | None = None
        if ephemeral_skill_name:
            ephemeral_skill = self.skills.load_skill(ephemeral_skill_name)

        runtime_ctx = self._build_runtime_context(channel, chat_id, active_resultset_id, session_datasets)
        user_content = self._build_user_content(current_message, media)

        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        system_content = self._build_system_content(memory_context, ephemeral_skill, company_dna, channel)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            *history,
            {"role": "user", "content": merged},
        ]
        if synthetic_turns:
            messages.extend(synthetic_turns)
        return messages

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            msg["thinking_blocks"] = thinking_blocks
        messages.append(msg)
        return messages
