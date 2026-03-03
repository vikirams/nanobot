"""Shared helpers for discovery-related tools (resolve which, payload parsing, CSV)."""

from __future__ import annotations

import csv
import io
import json
from typing import Any


def resolve_which(which: str) -> str | int | None:
    """
    Resolve 'which' parameter to 'last' or 1-based index.
    Returns None if invalid (e.g. non-numeric string).
    """
    w = (which or "").strip().lower()
    if w in ("last", ""):
        return "last"
    if w.isdigit() and int(w) >= 1:
        return int(w)
    return None


def rows_from_payload(payload: str) -> list[dict[str, Any]]:
    """
    Parse discovery payload JSON and return list of row dicts.
    Filters to only dict elements; supports both list and {data: list} shapes.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [r for r in data["data"] if isinstance(r, dict)]
    return []


def to_csv_string(rows: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    """Convert rows to CSV string. If columns is None, derive from row keys."""
    if not rows:
        return ""
    if columns is None:
        all_keys: dict[str, None] = {}
        for row in rows:
            if isinstance(row, dict):
                all_keys.update(dict.fromkeys(row.keys()))
        columns = list(all_keys)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        if isinstance(row, dict):
            writer.writerow({col: ("" if row.get(col) is None else row.get(col)) for col in columns})
    return buf.getvalue()
