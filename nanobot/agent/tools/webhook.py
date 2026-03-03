"""Webhook push tool: POST a stored discovery dataset to any HTTP endpoint as JSON.

Use for generic webhooks (Zapier, Make, n8n, custom APIs).
For Segment: use the MCP segment-push tool instead — it handles Segment's
identify/track protocol correctly and is already connected via MCP.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

import httpx

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.discovery_helpers import resolve_which, rows_from_payload
from nanobot.agent.tools.url_validation import validate_not_private

if TYPE_CHECKING:
    from nanobot.hybrid_memory.sqlite_manager import SqliteManager


class WebhookPushTool(Tool):
    """POST a stored discovery dataset as JSON to a webhook URL."""

    def __init__(self, sqlite_manager: Optional["SqliteManager"] = None) -> None:
        self._sqlite_manager = sqlite_manager
        self._session_key: Optional[str] = None

    def set_context(self, session_key: str, sqlite_manager: Any = None) -> None:
        self._session_key = session_key
        if sqlite_manager is not None:
            self._sqlite_manager = sqlite_manager

    @property
    def name(self) -> str:
        return "push_to_webhook"

    @property
    def description(self) -> str:
        return (
            "POST a stored discovery dataset to a webhook URL as a JSON array. "
            "Use when the user wants to push results to a CRM, Zapier, Make, n8n, "
            "or any HTTP endpoint that accepts JSON. "
            "For Segment: use the MCP segment push tool instead. "
            "Parameters: which ('last' or 1-based index), webhook_url (full URL), "
            "auth_header (optional: e.g. 'Bearer <token>' or 'ApiKey <key>')."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "which": {
                    "type": "string",
                    "description": "Which dataset: 'last' or 1-based index (e.g. '1')",
                },
                "webhook_url": {
                    "type": "string",
                    "description": "Target webhook URL (must start with https:// or http://)",
                },
                "auth_header": {
                    "type": "string",
                    "description": "Optional Authorization header value, e.g. 'Bearer sk-...'",
                },
            },
            "required": ["which", "webhook_url"],
        }

    async def execute(
        self,
        which: str = "last",
        webhook_url: str = "",
        auth_header: str = "",
        **kwargs: Any,
    ) -> str:
        if not self._sqlite_manager or not self._session_key:
            return "Error: Discovery storage not available."

        webhook_url = webhook_url.strip()
        if not webhook_url:
            return "Error: webhook_url is required."
        if not webhook_url.startswith(("http://", "https://")):
            return "Error: webhook_url must start with https:// or http://"
        allowed, ssrf_msg = validate_not_private(webhook_url)
        if not allowed:
            return f"Error: webhook_url not allowed — {ssrf_msg}"

        idx = resolve_which(which)
        if idx is None:
            return f"Error: 'which' must be 'last' or a positive integer, got '{which}'."

        try:
            row = await self._sqlite_manager.get_discovery_result(self._session_key, idx)
        except Exception as exc:
            return f"Error loading discovery result: {exc}"

        if not row:
            return f"No discovery result for which={which}. Run list_discovery_results first."

        _, payload, label = row
        records = rows_from_payload(payload)

        if not records:
            return "That discovery result contains no records to push."

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth_header.strip():
            headers["Authorization"] = auth_header.strip()

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    webhook_url,
                    json=records,
                    headers=headers,
                )
                status = resp.status_code
                body = resp.text[:300]
        except Exception as exc:
            return f"✗ Webhook POST failed: {exc}\nURL: {webhook_url}"

        if 200 <= status < 300:
            return (
                f"✓ Pushed {len(records)} records to webhook (HTTP {status}).\n"
                f"Dataset: {label or which}\n"
                f"URL: {webhook_url}"
            )
        return (
            f"✗ Webhook returned HTTP {status}.\n"
            f"Response: {body}\n"
            f"URL: {webhook_url}"
        )
