"""CSV export tool: convert tabular data and save to workspace for download."""

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class SaveCSVTool(Tool):
    """
    Save tabular data as a CSV file in the workspace and return a download link.

    Accepts data in any of these formats:
      - ClickHouse JSON:  {"meta": [{"name":..,"type":..},...], "data": [...]}
      - JSON array:       [{"col": value, ...}, ...]
      - Raw CSV string:   already-formatted CSV text

    Returns the /download/<filename> path so the agent can embed it as a
    markdown download link in its reply.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    # ── Tool ABC ───────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "save_csv"

    @property
    def description(self) -> str:
        return (
            "Save tabular data as a CSV file and return a /download/ link for the user. "
            "Call this whenever the user asks for a CSV or file download. "
            "Accepts: ClickHouse JSON ({\"meta\":[...],\"data\":[...]}), "
            "a JSON array of objects ([{...},...]), or plain CSV text."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "data": {
                    "type": "string",
                    "description": (
                        "The tabular data to save. "
                        "May be a ClickHouse-style JSON response, a JSON array of objects, "
                        "or a plain CSV string."
                    ),
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Output filename, e.g. 'companies.csv'. "
                        "Must end with .csv. Must not contain path separators."
                    ),
                },
            },
            "required": ["data", "filename"],
        }

    # ── Execution ──────────────────────────────────────────────────────────

    async def execute(self, data: str, filename: str, **kwargs: Any) -> str:
        # Validate filename
        if not filename:
            return "Error: filename is required."
        safe = filename.strip()
        if "/" in safe or "\\" in safe or ".." in safe:
            return "Error: filename must not contain path separators or '..'."
        if not safe.lower().endswith(".csv"):
            safe = safe + ".csv"
        # Inject timestamp before .csv to ensure unique filenames per export
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = safe[:-4] + f"_{ts}.csv"

        # Convert to CSV text
        csv_text = self._to_csv(data)
        if csv_text is None:
            return (
                "Error: could not parse 'data' as ClickHouse JSON, a JSON array of objects, "
                "or a CSV string. Check the format and try again."
            )

        # Write to workspace root (download endpoint serves from here)
        try:
            dest = self._workspace / safe
            dest.write_text(csv_text, encoding="utf-8")
        except Exception as exc:
            return f"Error writing '{safe}': {exc}"

        row_count = max(0, csv_text.count("\n") - 1)
        return (
            f"Saved {row_count} rows to '{safe}'. "
            f"Include EXACTLY this markdown link in your reply (relative URL, no protocol prefix): "
            f"[Download CSV](/download/{safe})  "
            f"IMPORTANT: do NOT add 'sandbox:', 'file://', or any other prefix — "
            f"the path /download/{safe} is served by the web UI HTTP server."
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _to_csv(raw: str) -> str | None:
        """
        Convert *raw* to a CSV string.

        Supported inputs:
          1. ClickHouse HTTP response: {"meta":[{"name":..},...], "data":[{..},...]}
          2. JSON array of objects:    [{..}, {..}, ...]
          3. Already-formatted CSV:    "col1,col2\nv1,v2\n..."

        Returns None if the input cannot be understood.
        """
        stripped = (raw or "").strip()
        if not stripped:
            return None

        # --- Try JSON ---
        if stripped[0] in ("{", "["):
            try:
                parsed = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                parsed = None

            if isinstance(parsed, dict):
                meta = parsed.get("meta")
                rows = parsed.get("data")
                if isinstance(meta, list) and isinstance(rows, list) and meta:
                    # ClickHouse format — use column order from meta
                    columns = [
                        col.get("name", str(i)) for i, col in enumerate(meta)
                    ]
                    return SaveCSVTool._write_csv(columns, rows)
                # Unified discovery format: {searchType, data: [...]} or any {data: [...]}
                if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                    all_keys: dict[str, None] = {}
                    for row in rows:
                        if isinstance(row, dict):
                            all_keys.update(dict.fromkeys(row.keys()))
                    return SaveCSVTool._write_csv(list(all_keys), rows)

            if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                # Plain JSON array — collect all keys, sort for determinism
                all_keys: dict[str, None] = {}
                for row in parsed:
                    if isinstance(row, dict):
                        all_keys.update(dict.fromkeys(row.keys()))
                return SaveCSVTool._write_csv(list(all_keys), parsed)

            # Parsed but not a recognised shape
            return None

        # --- Treat as raw CSV ---
        if "\n" in stripped and "," in stripped:
            return stripped

        return None

    @staticmethod
    def _write_csv(columns: list[str], rows: list[Any]) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if isinstance(row, dict):
                writer.writerow(
                    {col: ("" if row.get(col) is None else row.get(col)) for col in columns}
                )
        return buf.getvalue()
