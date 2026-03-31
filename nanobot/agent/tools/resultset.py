"""Tool for listing resultset_refs collected in the current session."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.db.manager import DBManager


class ListResultsetsTool(Tool):
    """List datasets collected in this session with their MCP-canonical IDs.

    Each result includes mcp_resultset_id — the ID to pass to hp.exportCsv(),
    hp.filterContacts(), hp.mergeResultsets(), etc.
    """

    name = "list_resultsets"
    description = (
        "List all datasets collected in this session. "
        "Each entry includes mcp_resultset_id (use this with hp.exportCsv(), "
        "hp.filterContacts(), etc.), label, and row_count."
    )
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": (
                    "Session ID to list resultsets for. "
                    "If omitted, uses the current session."
                ),
            }
        },
        "required": [],
    }

    def __init__(self, db_manager: Optional["DBManager"] = None) -> None:
        self._db_manager = db_manager
        self._session_id: str = ""
        self._account_id: str = ""
        self._user_id: str = ""
        self._active_resultset_id: str = ""

    def set_context(
        self,
        session_id: str,
        db_manager: "DBManager | None" = None,
        account_id: str = "",
        user_id: str = "",
        active_resultset_id: str = "",
    ) -> None:
        """Inject runtime context (session, db_manager, account_id, user_id)."""
        self._session_id = session_id
        self._account_id = account_id
        self._user_id = user_id
        self._active_resultset_id = active_resultset_id
        if db_manager is not None:
            self._db_manager = db_manager

    async def execute(self, session_id: str = "", **kwargs: Any) -> str:
        if self._db_manager is None:
            return json.dumps({"error": "DBManager not configured — Postgres path not active."})

        session_id = session_id or self._session_id
        if not session_id:
            return json.dumps({"error": "No session_id available."})
        if not self._account_id:
            return json.dumps({"error": "No account_id available."})
        if not self._user_id:
            return json.dumps({"error": "No user_id available."})

        try:
            refs = await self._db_manager.list_resultset_refs(
                account_id=self._account_id,
                user_id=self._user_id,
                session_id=session_id,
                limit=20,
            )
        except Exception as exc:
            return json.dumps({"error": f"Failed to list resultsets: {exc}"})

        if not refs:
            if self._active_resultset_id:
                return json.dumps({
                    "message": (
                        "No stored resultsets found in session history. "
                        "The active dataset below was fetched via slash command."
                    ),
                    "resultsets": [],
                    "active_mcp_resultset_id": self._active_resultset_id,
                    "action": (
                        f"Use mcp_resultset_id={self._active_resultset_id!r} "
                        "with hp.exportCsv() or hp.filterContacts()."
                    ),
                })
            return json.dumps({
                "message": "No data results have been collected in this session yet.",
                "resultsets": [],
            })

        # Build clean entries with mcp_resultset_id as the primary action field
        formatted = []
        for r in refs:
            item = dict(r)
            if hasattr(item.get("created_at"), "isoformat"):
                item["created_at"] = item["created_at"].isoformat()
            # Normalise row_count to int when possible
            if item.get("row_count") is not None:
                try:
                    item["row_count"] = int(item["row_count"])
                except (ValueError, TypeError):
                    pass
            # Remove content_preview — no longer returned; mcp_resultset_id is the signal
            item.pop("content_preview", None)
            formatted.append(item)

        result: dict[str, Any] = {
            "resultsets": formatted,
            "count": len(formatted),
            "action_guide": (
                "Use mcp_resultset_id with MCP tools: hp.exportCsv(mcp_resultset_id), "
                "hp.filterContacts(), hp.mergeResultsets(), etc. "
                "resultset_ref values prefixed 'agent_' are internal DB keys — never pass them to MCP tools."
            ),
        }
        if self._active_resultset_id:
            result["active_mcp_resultset_id"] = self._active_resultset_id
        return json.dumps(result, ensure_ascii=False)
