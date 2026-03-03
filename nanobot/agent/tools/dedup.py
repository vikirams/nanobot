"""Deduplication tool: merge two stored discovery datasets into one.

Operates entirely in-process (Python dict operations) — no external service needed.
Even 10,000 records dedup in <50ms on a modern CPU, so a Vercel sandbox would
only add latency and cost without benefit.

Deduplication strategy (priority-ordered):
  1. email match (case-insensitive, stripped)
  2. linkedin_url match
  3. No key found → record treated as unique

When a duplicate is found the richer record wins:
  - Keep A's record as the base
  - Fill any of A's null/empty fields with B's non-null values (never overwrite)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Optional

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.hybrid_memory.sqlite_manager import SqliteManager


class DeduplicateTool(Tool):
    """Merge and deduplicate two stored discovery datasets into one."""

    def __init__(self, sqlite_manager: Optional["SqliteManager"] = None) -> None:
        self._sqlite_manager = sqlite_manager
        self._session_key: Optional[str] = None

    def set_context(self, session_key: str, sqlite_manager: Any = None) -> None:
        self._session_key = session_key
        if sqlite_manager is not None:
            self._sqlite_manager = sqlite_manager

    @property
    def name(self) -> str:
        return "deduplicate_results"

    @property
    def description(self) -> str:
        return (
            "Merge and deduplicate two stored discovery datasets into one unified dataset. "
            "Use after running both DB discovery (dataset 1) and deep research (dataset 2) "
            "to combine and remove duplicates. "
            "Dedup uses email first, then linkedin_url. The richer record always wins (no data lost). "
            "The merged result is stored as a new dataset — use export_discovery_to_csv or "
            "push_to_webhook on the result. "
            "Parameters: which_a (first dataset, e.g. '1'), which_b (second dataset, e.g. '2' or 'last'), "
            "dedup_keys (priority-ordered list, default ['email','linkedin_url']), "
            "label (name for the merged dataset)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "which_a": {
                    "type": "string",
                    "description": "First dataset: 'last' or 1-based index (e.g. '1')",
                },
                "which_b": {
                    "type": "string",
                    "description": "Second dataset: 'last' or 1-based index (e.g. '2')",
                },
                "dedup_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Fields to use as dedup keys in priority order. "
                        "Defaults to ['email', 'linkedin_url']. "
                        "Use ['domain'] for company dedup."
                    ),
                },
                "label": {
                    "type": "string",
                    "description": "Label for the merged dataset, e.g. 'India CTOs merged'",
                },
            },
            "required": ["which_a", "which_b"],
        }

    async def execute(
        self,
        which_a: str = "1",
        which_b: str = "2",
        dedup_keys: Optional[list] = None,
        label: str = "",
        **kwargs: Any,
    ) -> str:
        if not self._sqlite_manager or not self._session_key:
            return "Error: Discovery storage not available (session or database not initialised)."

        keys = dedup_keys if dedup_keys else ["email", "linkedin_url"]

        from nanobot.agent.tools.discovery_helpers import resolve_which, rows_from_payload
        idx_a = resolve_which(which_a) or "last"
        idx_b = resolve_which(which_b) or "last"

        try:
            row_a = await self._sqlite_manager.get_discovery_result(self._session_key, idx_a)
            row_b = await self._sqlite_manager.get_discovery_result(self._session_key, idx_b)
        except Exception as exc:
            return f"Error loading datasets: {exc}"

        if not row_a:
            return f"Dataset '{which_a}' not found. Run list_discovery_results to see available datasets."
        if not row_b:
            return f"Dataset '{which_b}' not found. Run list_discovery_results to see available datasets."

        _, payload_a, label_a = row_a
        _, payload_b, label_b = row_b

        records_a = rows_from_payload(payload_a)
        records_b = rows_from_payload(payload_b)

        if not records_a and not records_b:
            return "Both datasets are empty — nothing to merge."

        merged, dupe_count = _merge_dedup(records_a, records_b, keys)

        merged_label = label.strip() or f"Merged: {label_a or ('Dataset ' + which_a)} + {label_b or ('Dataset ' + which_b)}"
        merged_payload = json.dumps(merged, ensure_ascii=False)

        try:
            await self._sqlite_manager.insert_discovery_result(
                session_id=self._session_key,
                tool_name="deduplicate_results",
                payload=merged_payload,
                query_or_label=merged_label,
                shape="array",
                row_count=len(merged),
            )
        except Exception as exc:
            return f"Error storing merged result: {exc}"

        return (
            f"✓ Merge complete\n"
            f"  Dataset A — '{label_a or which_a}': {len(records_a)} records\n"
            f"  Dataset B — '{label_b or which_b}': {len(records_b)} records\n"
            f"  Duplicates removed: {dupe_count}\n"
            f"  Merged dataset: **{len(merged)} unique records** (saved as '{merged_label}')\n\n"
            f"Use export_discovery_to_csv(which='last') to download or "
            f"push_to_webhook(which='last', webhook_url=...) to push."
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _merge_dedup(
    records_a: list[dict],
    records_b: list[dict],
    dedup_keys: list[str],
) -> tuple[list[dict], int]:
    """Merge A + B, dedup on priority-ordered keys.

    Returns (merged_records, duplicate_count).
    Records in A are always kept. Records in B are added only if their
    fingerprint is not already in A; matching records' data is merged into A's.
    """

    def _fingerprint(record: dict) -> str | None:
        for key in dedup_keys:
            val = str(record.get(key) or "").strip().lower()
            if val:
                return f"{key}:{val}"
        return None

    # Index all of A by fingerprint (keyed records and unkeyed separately)
    seen: dict[str, dict] = {}
    unkeyed_a: list[dict] = []

    for rec in records_a:
        fp = _fingerprint(rec)
        if fp:
            seen[fp] = dict(rec)  # copy so we don't mutate caller's data
        else:
            unkeyed_a.append(dict(rec))

    dupe_count = 0
    b_new: list[dict] = []

    for rec in records_b:
        fp = _fingerprint(rec)
        if fp and fp in seen:
            # Duplicate — enrich A's record with B's non-null fields
            existing = seen[fp]
            for k, v in rec.items():
                if v is not None and v != "" and (existing.get(k) is None or existing.get(k) == ""):
                    existing[k] = v
            dupe_count += 1
        else:
            b_copy = dict(rec)
            b_new.append(b_copy)
            if fp:
                seen[fp] = b_copy

    # Final order: keyed A records (enriched) + unkeyed A + new B records
    merged = list(seen.values()) + unkeyed_a + [r for r in b_new if not _fingerprint(r)]
    return merged, dupe_count
