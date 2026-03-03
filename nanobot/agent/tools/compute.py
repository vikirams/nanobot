"""Data analysis tool: run agent-generated Python code against a discovery dataset.

Execution is always via a subprocess: dataset rows are piped via stdin as UTF-8
JSON. Credential env vars are stripped before the child starts. Hard timeout: 30 s.

Usage pattern in agent-generated code:
    import json, sys
    data = json.load(sys.stdin)
    # compute …
    print(result)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.discovery_helpers import resolve_which

if TYPE_CHECKING:
    from nanobot.config.schema import SandboxConfig
    from nanobot.hybrid_memory.sqlite_manager import SqliteManager


# ---------------------------------------------------------------------------
# Subprocess backend helpers
# ---------------------------------------------------------------------------

from nanobot.agent.tools.env_safe import build_safe_env as _env_safe_build


def _build_safe_env() -> dict[str, str]:
    """Return a minimal env dict with all credential vars stripped."""
    return _env_safe_build(extra={"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class AnalyzeDiscoveryTool(Tool):
    """Execute Python code against the current discovery dataset in a subprocess.

    Credential env vars are stripped; full isolation requires container/sandbox (e.g. Docker).
    """

    _MAX_OUTPUT = 8_000
    _TIMEOUT = 30  # seconds

    def __init__(
        self,
        sqlite_manager: "SqliteManager",
        sandbox_config: "SandboxConfig | None" = None,
    ) -> None:
        self._sqlite_manager = sqlite_manager
        self._session_key: str = ""
        self._on_progress: Callable[[str], Awaitable[None]] | None = None
        self._sandbox_config = sandbox_config

    def set_context(
        self,
        session_key: str,
        *,
        sqlite_manager: "SqliteManager | None" = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        sandbox_config: "SandboxConfig | None" = None,
        **_: Any,
    ) -> None:
        self._session_key = session_key
        if sqlite_manager is not None:
            self._sqlite_manager = sqlite_manager
        if on_progress is not None:
            self._on_progress = on_progress
        if sandbox_config is not None:
            self._sandbox_config = sandbox_config

    @property
    def name(self) -> str:
        return "analyze_discovery_data"

    @property
    def description(self) -> str:
        return (
            "Run Python code to analyze the full discovery dataset. "
            "Use this for ALL counting, filtering, listing rows by criteria, grouping, "
            "top-N ranking, statistics, or any computation — this is the only analysis tool. "
            "The full dataset is piped to the subprocess via stdin as a JSON array of dicts. "
            "Read it with: import json, sys; data = json.load(sys.stdin). "
            "Print the result to stdout. Import only stdlib modules. "
            "Example:\n"
            "  import json, sys, collections\n"
            "  data = json.load(sys.stdin)\n"
            "  counts = collections.Counter(r.get('industry','') for r in data)\n"
            "  print(json.dumps(dict(counts.most_common(10)), indent=2))"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Self-contained Python code. "
                        "Read the full dataset: import json, sys; data = json.load(sys.stdin). "
                        "Print the result to stdout. Import only stdlib modules."
                    ),
                },
                "which": {
                    "type": "string",
                    "description": "Which dataset: 'last' (default) or 1-based index like '1', '2'.",
                    "default": "last",
                },
            },
            "required": ["code"],
        }

    # ------------------------------------------------------------------
    # Dataset loader
    # ------------------------------------------------------------------

    async def _load_dataset(self, which: str) -> list[dict] | None:
        which_idx = resolve_which(which)
        if which_idx is None:
            return None
        row = await self._sqlite_manager.get_discovery_result(
            session_id=self._session_key, which=which_idx
        )
        if not row:
            return None
        _, payload_json, _ = row
        try:
            data = json.loads(payload_json)
        except json.JSONDecodeError:
            return None
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return None

    async def _run_subprocess(
        self,
        code: str,
        stdin_data: bytes,
        on_progress: Callable[[str], Awaitable[None]] | None,
    ) -> tuple[str, str, int]:
        """Run code in a credential-stripped child process, streaming stdout."""
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        total_chars = 0

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_safe_env(),
        )

        async def _write_stdin() -> None:
            if stdin_data and proc.stdin:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()

        async def _stream_stdout() -> None:
            nonlocal total_chars
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n\r")
                stdout_lines.append(decoded)
                total_chars += len(decoded)
                if decoded and on_progress and total_chars <= self._MAX_OUTPUT:
                    await on_progress(f"⚙ {decoded}")

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_lines.append(line.decode("utf-8", errors="replace").rstrip("\n\r"))

        try:
            await asyncio.wait_for(
                asyncio.gather(_write_stdin(), _stream_stdout(), _drain_stderr()),
                timeout=self._TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            raise

        await proc.wait()
        return "\n".join(stdout_lines), "\n".join(stderr_lines), proc.returncode or 0

    # ------------------------------------------------------------------
    # Public execute
    # ------------------------------------------------------------------

    async def execute(self, code: str = "", which: str = "last", **_: Any) -> str:
        if not code.strip():
            return "Error: 'code' parameter is required."
        if not self._session_key:
            return "Error: session context not set."

        rows = await self._load_dataset(which)
        if rows is None:
            return (
                "Error: No discovery dataset found. "
                "Run a discovery first, then call this tool."
            )

        on_progress = self._on_progress
        if on_progress:
            await on_progress(f"⚙ Running analysis on {len(rows)} records…")

        stdin_data = json.dumps(rows, ensure_ascii=False).encode("utf-8")

        try:
            stdout, stderr, returncode = await asyncio.wait_for(
                self._run_subprocess(code, stdin_data, on_progress),
                timeout=self._TIMEOUT,
            )
        except asyncio.TimeoutError:
            return f"Error: Code execution timed out after {self._TIMEOUT}s."

        if returncode != 0:
            return (
                f"Error (exit {returncode}):\n"
                + (stderr[:2_000] if stderr else "(no stderr)")
            )

        if not stdout:
            return "(No output produced — add print() to display results.)"

        if len(stdout) > self._MAX_OUTPUT:
            stdout = stdout[: self._MAX_OUTPUT] + "\n... [output truncated]"

        return stdout
