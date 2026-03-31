"""Prepare conversation history for the LLM: replace large tool results with context-preserving placeholders."""

from __future__ import annotations

import json
from typing import Any, Optional

# Above this length, tool result content is replaced with a placeholder so the model keeps context without blowing the window.
MAX_TOOL_RESULT_CHARS = 2000

# Minimum payload size to bother storing in discovery_results (avoids {"status": "ok"} noise).
_MIN_JSON_STORE_CHARS = 100

# Field names that strongly indicate a contact or company discovery result.
# Normalised to lowercase-no-separator for comparison.
# Covers both flat schemas and HP API nested-attributes shapes.
_CONTACT_COMPANY_SIGNALS: frozenset[str] = frozenset({
    # Flat / legacy field names
    "email", "linkedinurl", "firstname", "lastname", "jobtitle", "title",
    "phone", "mobile", "companyname", "company", "website", "domain",
    "companydomain", "companywebsite", "companyurl", "contactid", "personid",
    "hpcontactid", "hpcompanyid",
    # HP API nested-attributes field names (inside attributes: {...})
    "fullname", "linkedin", "emails", "phonenumbers",
})

# Field names that strongly indicate a management-list result (segments, sources,
# workflow tasks, etc.) — these are NOT contacts/companies.
_MANAGEMENT_SIGNALS: frozenset[str] = frozenset({
    "membercount", "segmentid", "sourcetype", "sourcemeta", "templateid",
    "workflowstatus", "conditions", "fieldmapping", "segmentfolder",
    "workflowtaskid", "runstatus",
    # HP API nested-attributes field names for management entities
    "segmentsources", "groupedcondition", "colourcode", "icp", "icpconfiguration",
})

# HP API `type` field values that identify contact/company rows.
_CONTACT_COMPANY_TYPES: frozenset[str] = frozenset({
    "contact", "person", "company", "organization", "lead",
})


def _normalise_key(k: str) -> str:
    """Lowercase and strip separators for field signal matching."""
    return k.lower().replace("_", "").replace("-", "")


def classify_result_kind(rows: list[dict]) -> str:
    """Return 'contact_company' or 'management_list' based on first-row field names.

    contact_company → contacts or companies from discovery APIs.
                      Eligible for [Preview] sentinel and CSV download.
    management_list → segments, sources, workflow tasks, schema lists, etc.
                      Rendered inline by the agent; no preview sentinel, no CSV download.

    Detection strategy (in priority order):
    1. If a `type` field is present and its value matches a known contact/company type → contact_company.
    2. Check top-level keys against signal sets.
    3. Recurse one level into an `attributes` dict (HP API nests everything there).
    """
    if not rows or not isinstance(rows[0], dict):
        return "management_list"

    row = rows[0]

    # 1. type-field shortcut (HP API always includes type: "contact" | "segment" | ...)
    type_val = row.get("type")
    if isinstance(type_val, str) and type_val.lower() in _CONTACT_COMPANY_TYPES:
        return "contact_company"

    # 2. Top-level key signal match
    keys = {_normalise_key(k) for k in row}
    discovery_hits = len(keys & _CONTACT_COMPANY_SIGNALS)
    management_hits = len(keys & _MANAGEMENT_SIGNALS)

    if discovery_hits > management_hits:
        return "contact_company"
    if management_hits > discovery_hits:
        return "management_list"

    # 3. Recurse into `attributes` dict (HP API nesting)
    attrs = row.get("attributes")
    if isinstance(attrs, dict):
        attr_keys = {_normalise_key(k) for k in attrs}
        attr_discovery = len(attr_keys & _CONTACT_COMPANY_SIGNALS)
        attr_management = len(attr_keys & _MANAGEMENT_SIGNALS)
        if attr_discovery > attr_management:
            return "contact_company"
        if attr_management > attr_discovery:
            return "management_list"

    # Default: treat as management list (safer — no CSV download shown)
    return "management_list"


def _extract_rows(data: Any) -> list[dict]:
    """Return the row list from parsed JSON regardless of envelope shape."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        # "preview" is the MCP execute envelope key for stored resultset responses
        for key in ("results", "data", "items", "records", "preview"):
            rows = data.get(key)
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return rows
    return []


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
    """
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


def classify_json_result(
    name: str, content: str, parsed: Any = None
) -> Optional[tuple[str, int]]:
    """Classify a tool result for storage in discovery_results.

    Returns (shape, row_count) if the content is valid JSON worth storing, else None.
    If parsed is provided (already-loaded JSON), skips json.loads.

    shape values:
      "array"   — direct JSON array of objects: [{...}, ...]
      "wrapped" — object with a results/data/items key containing an array
      "object"  — any other JSON object: {"key": "value", ...}

    row_count is the number of rows for array/wrapped shapes, 0 for plain objects.

    Rejects:
      - Payloads shorter than _MIN_JSON_STORE_CHARS (avoids {"status": "ok"} noise)
      - Non-JSON content (plain text, HTML, etc.)
      - Empty arrays / arrays of non-objects
    """
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
        # Check all common envelope keys, not just "data"
        for key in ("results", "data", "items", "records"):
            rows = data.get(key)
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                return ("wrapped", len(rows))
        # Plain object — only store if it has meaningful keys (not just a status wrapper)
        if len(data) >= 2:
            return ("object", 0)

    return None


def get_result_kind(content: str, parsed: Any = None) -> str:
    """Return 'contact_company' or 'management_list' for a stored tabular result.

    Used by loop.py to decide whether to emit the [Preview] sentinel hint and
    whether the result is eligible for CSV download.
    Falls back to 'management_list' when content cannot be parsed.
    """
    try:
        data = parsed if parsed is not None else json.loads((content or "").strip())
    except Exception:
        return "management_list"

    rows = _extract_rows(data)
    return classify_result_kind(rows)


def make_discovery_label(tool_name: str, shape: str, row_count: int, content: str) -> str:
    """Generate a human-readable label for a discovery result.

    Used both for display (list_discovery_results) and as the pgvector embedding text
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
                        "[JSON result (large). Use list_resultsets to see all datasets collected in this session.]"
                    )
                else:
                    entry["content"] = (
                        "[Large tool result omitted. Use tools to inspect or re-run if needed.]"
                    )
        out.append(entry)
    return out
