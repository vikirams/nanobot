"""Discovery result tools: list, export to CSV, and query stored MCP discovery results."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.discovery_helpers import resolve_which, rows_from_payload, to_csv_string

if TYPE_CHECKING:
    from nanobot.hybrid_memory.sqlite_manager import SqliteManager


class ListDiscoveryResultsTool(Tool):
    """List discovery results for the current session so the user can pick one by index or 'last'."""

    def __init__(self, sqlite_manager: Optional[SqliteManager] = None) -> None:
        self._sqlite_manager = sqlite_manager
        self._session_key: Optional[str] = None

    def set_context(self, session_key: str, sqlite_manager=None) -> None:
        self._session_key = session_key
        if sqlite_manager is not None:
            self._sqlite_manager = sqlite_manager

    @property
    def name(self) -> str:
        return "list_discovery_results"

    @property
    def description(self) -> str:
        return (
            "List discovery results for this session. Use when the user has run discovery multiple times "
            "and you need to show which datasets exist (e.g. 'USA companies', 'India companies') so they can "
            "choose which to export or analyze. Returns id, index, label, row count, created_at."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        if not self._sqlite_manager or not self._session_key:
            return "Error: Discovery storage is not available (session or database not set)."
        try:
            items = await self._sqlite_manager.list_discovery_results(self._session_key)
        except Exception as e:
            return f"Error listing discovery results: {e}"
        if not items:
            return "No discovery results in this session. Run a discovery first."
        lines = [f"{i['index']}. {i['label']} — {i['rows']} rows ({i['created_at'][:19] if i.get('created_at') else '?'})" for i in items]
        return "Discovery results in this session:\n" + "\n".join(lines) + "\n\nUse export_discovery_to_csv(which=N) to export, or analyze_discovery_data(code='...', which=N) to run analysis."


class ExportDiscoveryToCsvTool(Tool):
    """Export a stored discovery result to a CSV file. which = 'last' or 1-based index."""

    def __init__(self, sqlite_manager: Optional[SqliteManager] = None, workspace: Optional[Path] = None) -> None:
        self._sqlite_manager = sqlite_manager
        self._workspace = workspace or Path(".")
        self._session_key: Optional[str] = None

    def set_context(self, session_key: str, sqlite_manager=None) -> None:
        self._session_key = session_key
        if sqlite_manager is not None:
            self._sqlite_manager = sqlite_manager

    @property
    def name(self) -> str:
        return "export_discovery_to_csv"

    @property
    def description(self) -> str:
        return (
            "Export a previously run discovery result to a CSV file. Use when the user asks to download or export "
            "discovery data. which: 'last' (most recent) or 1-based index (1, 2, 3…). filename_prefix: e.g. 'companies'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "which": {
                    "type": "string",
                    "description": "Which result: 'last' or 1-based index as string, e.g. '1', '2'.",
                },
                "filename_prefix": {
                    "type": "string",
                    "description": "Filename prefix for the CSV, e.g. 'companies'. Will get a timestamp and .csv suffix.",
                },
            },
            "required": ["which", "filename_prefix"],
        }

    async def execute(
        self,
        which: str = "last",
        filename_prefix: str = "discovery",
        **kwargs: Any,
    ) -> str:
        if not self._sqlite_manager or not self._session_key:
            return "Error: Discovery storage is not available."
        idx = resolve_which(which)
        if idx is None:
            return f"Error: 'which' must be 'last' or a positive integer (1, 2, …), got '{which}'."
        try:
            row = await self._sqlite_manager.get_discovery_result(self._session_key, idx)
        except Exception as e:
            return f"Error loading discovery result: {e}"
        if not row:
            return f"No discovery result for which={which}. Use list_discovery_results to see available results."
        _, payload, _ = row
        rows = rows_from_payload(payload)
        if not rows:
            return "That discovery result has no rows to export."
        safe = (filename_prefix or "discovery").strip()
        if "/" in safe or "\\" in safe or ".." in safe:
            return "Error: filename prefix must not contain path separators or '..'."
        if not safe.lower().endswith(".csv"):
            safe = safe + ".csv"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = safe[:-4] + f"_{ts}.csv"
        csv_text = to_csv_string(rows)
        try:
            dest = self._workspace / safe
            dest.write_text(csv_text, encoding="utf-8")
        except Exception as exc:
            return f"Error writing '{safe}': {exc}"
        row_count = len(rows)
        return (
            f"Saved {row_count} rows to '{safe}'. "
            f"Include this link in your reply: [Download CSV](/download/{safe})"
        )


class GetDiscoveryDataTool(Tool):
    """Return rows (or count/summary) from a stored discovery result for analysis. which = 'last' or index."""

    def __init__(self, sqlite_manager: Optional[SqliteManager] = None) -> None:
        self._sqlite_manager = sqlite_manager
        self._session_key: Optional[str] = None

    def set_context(self, session_key: str, sqlite_manager=None) -> None:
        self._session_key = session_key
        if sqlite_manager is not None:
            self._sqlite_manager = sqlite_manager

    @property
    def name(self) -> str:
        return "get_discovery_data"

    @property
    def description(self) -> str:
        return (
            "Show a PREVIEW of a stored discovery result. Use ONLY to show the user a sample of the data "
            "or to check column names. DO NOT use for counting, filtering, listing rows by criteria, "
            "grouping, ranking, or any selection/analysis — even if the user asks to 'list all X' or 'show all Y'. "
            "Returns at most 20 rows and will miss data from larger datasets. "
            "For ALL filtering, listing, counting, ranking, or computation use analyze_discovery_data with Python code."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "which": {
                    "type": "string",
                    "description": "Which result: 'last' or 1-based index, e.g. '1', '2'.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: column names to include. Omit for all columns.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 20 for preview). Increase only if the user explicitly asks for more rows.",
                    "default": 20,
                },
            },
            "required": ["which"],
        }

    async def execute(
        self,
        which: str = "last",
        columns: Optional[list[str]] = None,
        limit: Optional[int] = 20,
        **kwargs: Any,
    ) -> str:
        if not self._sqlite_manager or not self._session_key:
            return "Error: Discovery storage is not available."
        idx = resolve_which(which)
        if idx is None:
            return f"Error: 'which' must be 'last' or a positive integer (1, 2, …), got '{which}'."
        try:
            row = await self._sqlite_manager.get_discovery_result(self._session_key, idx)
        except Exception as e:
            return f"Error loading discovery result: {e}"
        if not row:
            return f"No discovery result for which={which}. Use list_discovery_results to see available results."
        _, payload, _ = row
        rows = rows_from_payload(payload)
        if not rows:
            return "That discovery result has no rows."
        if columns:
            rows = [{k: r.get(k) for k in columns if k in r} for r in rows]
        if limit is not None and limit > 0:
            rows = rows[:limit]
        try:
            return json.dumps(rows, ensure_ascii=False, indent=0)
        except Exception:
            return json.dumps(rows, ensure_ascii=False)