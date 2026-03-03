"""Prepare conversation history for the LLM: replace large tool results with context-preserving placeholders."""

from __future__ import annotations

import json
from typing import Any, Optional

# Above this length, tool result content is replaced with a placeholder so the model keeps context without blowing the window.
MAX_TOOL_RESULT_CHARS = 2000

# Local tools that READ from discovery storage — never write to it.
# These must never be mistaken for tools that produce new tabular data.
_LOCAL_READER_TOOLS = ("list_discovery", "get_discovery", "export_discovery", "save_csv")

# Minimum payload size to bother storing in discovery_results (avoids {"status": "ok"} noise).
_MIN_JSON_STORE_CHARS = 100


def is_tabular_tool_result(name: str, content: str) -> bool:
    """True if this tool result is bulk tabular data worth storing in discovery_results.

    Detection is content-based (JSON array of objects), NOT name-based.
    This means it automatically works for any MCP tool — no config needed when
    adding new APIs.

    The only name-based check is to exclude local utility tools that READ from
    discovery storage (so we don't store their output back into storage).

    Matches:
      [{"id": 1, "name": "Acme"}, ...]           ← direct JSON array of objects
      {"data": [...], "total": 100}               ← wrapped MCP format
      {"meta": [...], "data": [...]}              ← ClickHouse format

    Rejects:
      {"status": "ok"}                            ← not tabular
      ["string1", "string2"]                      ← not objects
      get_discovery_data result                   ← local reader, excluded by name
    """
    # Exclude local tools that read FROM storage (not MCP-produced output)
    n = (name or "").lower()
    if n.startswith(_LOCAL_READER_TOOLS):
        return False

    stripped = (content or "").strip()
    if not stripped or stripped[0] not in ("{", "["):
        return False

    try:
        data = json.loads(stripped)
    except Exception:
        return False

    # Direct JSON array of objects: [{...}, ...]
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return True

    # Wrapped format: {"data": [{...}, ...], ...} (covers ClickHouse and MCP variants)
    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return True

    return False


def classify_json_result(name: str, content: str, parsed: Any = None) -> Optional[tuple[str, int]]:
    """Classify a tool result for storage in discovery_results.

    Returns (shape, row_count) if the content is valid JSON worth storing, else None.
    If parsed is provided (already-loaded JSON), skips json.loads.

    shape values:
      "array"   — direct JSON array of objects: [{...}, ...]
      "wrapped" — object with a "data" key containing an array: {"data": [{...}]}
      "object"  — any other JSON object: {"key": "value", ...}

    row_count is the number of rows for array/wrapped shapes, 0 for plain objects.

    Rejects:
      - Local reader tools (list_discovery, get_discovery, export_discovery, save_csv)
      - Payloads shorter than _MIN_JSON_STORE_CHARS (avoids {"status": "ok"} noise)
      - Non-JSON content (plain text, HTML, etc.)
      - Empty arrays / arrays of non-objects
    """
    n = (name or "").lower()
    if any(n.startswith(r) for r in _LOCAL_READER_TOOLS):
        return None

    stripped = (content or "").strip()
    if not stripped or len(stripped) < _MIN_JSON_STORE_CHARS:
        return None
    if stripped[0] not in ("{", "["):
        return None

    if parsed is not None:
        data = parsed
    else:
        try:
            data = json.loads(stripped)
        except Exception:
            return None

    if isinstance(data, list):
        if not data:
            return None
        row_count = len(data) if isinstance(data[0], dict) else 0
        return ("array", row_count)

    if isinstance(data, dict):
        rows = data.get("data")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return ("wrapped", len(rows))
        # Plain object — only store if it has meaningful keys (not just a status wrapper)
        if len(data) >= 2:
            return ("object", 0)

    return None


def make_discovery_label(tool_name: str, shape: str, row_count: int, content: str) -> str:
    """Generate a human-readable label for a discovery result.

    Used both for display (list_discovery_results) and as the zvec embedding text
    so cross-session semantic search can find datasets by description.

    Examples:
      "hp-discovery: 142 rows [id, name, country, revenue]"
      "crm-contacts: 37 rows [email, first_name, last_name, company]"
      "status-api: {health, version, uptime}"
    """
    # Shorten MCP tool names: "mcp_hp-discovery_hp_discovery" → "hp-discovery"
    parts = tool_name.split("_")
    short_name = parts[1] if len(parts) >= 2 else tool_name

    try:
        data = json.loads(content)
        if shape == "wrapped":
            data = data.get("data", [])

        if shape in ("array", "wrapped") and isinstance(data, list) and data:
            if isinstance(data[0], dict):
                keys = list(data[0].keys())[:5]
                return f"{short_name}: {row_count} rows [{', '.join(keys)}]"
            return f"{short_name}: {row_count} items"

        if shape == "object" and isinstance(data, dict):
            keys = list(data.keys())[:5]
            return f"{short_name}: {{{', '.join(keys)}}}"
    except Exception:
        pass

    return f"{short_name}: {row_count} rows" if row_count else short_name


# Backward-compat alias used by loop.py — now delegates to content-based check.
# Callers in loop.py should migrate to is_tabular_tool_result(name, content).
def is_discovery_tool(name: str) -> bool:
    """Deprecated: name-only check kept for callers that don't have content.
    Prefer is_tabular_tool_result(name, content) for accurate detection.
    """
    if not name:
        return False
    n = name.lower()
    if n.startswith(_LOCAL_READER_TOOLS):
        return False
    return "discovery" in n


def prepare_history_for_llm(
    history: list[dict[str, Any]],
    *,
    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
) -> list[dict[str, Any]]:
    """
    Return a copy of history where large tool result content is replaced with short placeholders.
    Preserves message structure and ordering; only the body of tool results is replaced.

    Uses content-based detection (is_tabular_tool_result) so the discovery placeholder
    is shown for ANY MCP tool that returned tabular data, not just ones with "discovery"
    in the name.
    """
    out: list[dict[str, Any]] = []
    for m in history:
        entry = dict(m)
        if entry.get("role") == "tool" and isinstance(entry.get("content"), str):
            content = entry["content"]
            if len(content) > max_tool_result_chars:
                name = entry.get("name") or ""
                if classify_json_result(name, content) is not None:
                    entry["content"] = (
                        "[JSON result (large). Use list_discovery_results to see all datasets; "
                        "get_discovery_data(which=N) or export_discovery_to_csv(which=N) to query or download.]"
                    )
                else:
                    entry["content"] = (
                        "[Large tool result omitted. Use tools to inspect or re-run if needed.]"
                    )
        out.append(entry)
    return out
