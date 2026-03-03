"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _dir_mtime(path: Path) -> float:
    """Max mtime of directory and its SKILL.md children (for cache invalidation)."""
    if not path.exists():
        return 0.0
    try:
        m = path.stat().st_mtime
    except OSError:
        return 0.0
    if path.is_dir():
        for child in path.iterdir():
            if child.is_dir():
                skill_file = child / "SKILL.md"
                if skill_file.exists():
                    try:
                        m = max(m, skill_file.stat().st_mtime)
                    except OSError:
                        pass
            try:
                m = max(m, child.stat().st_mtime)
            except OSError:
                pass
    return m


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    Caches list_skills, always-skills content, and summary with mtime-based invalidation.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self._cache_mtime: float = 0.0
        self._cache_list: list[dict[str, str]] | None = None
        self._cache_always: list[str] | None = None
        self._cache_always_content: str | None = None
        self._cache_summary: str | None = None

    def _skills_mtime(self) -> float:
        """Current max mtime of skills dirs for cache invalidation."""
        return max(
            _dir_mtime(self.workspace_skills),
            _dir_mtime(self.builtin_skills) if self.builtin_skills else 0.0,
        )

    def _ensure_cache(self) -> None:
        current = self._skills_mtime()
        if current > self._cache_mtime:
            self._cache_mtime = current
            self._cache_list = None
            self._cache_always = None
            self._cache_always_content = None
            self._cache_summary = None

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        self._ensure_cache()
        if self._cache_list is None:
            skills = []
            if self.workspace_skills.exists():
                for skill_dir in self.workspace_skills.iterdir():
                    if skill_dir.is_dir():
                        skill_file = skill_dir / "SKILL.md"
                        if skill_file.exists():
                            skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})
            if self.builtin_skills and self.builtin_skills.exists():
                for skill_dir in self.builtin_skills.iterdir():
                    if skill_dir.is_dir():
                        skill_file = skill_dir / "SKILL.md"
                        if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                            skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})
            self._cache_list = skills
        skills = self._cache_list
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return list(skills)

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        # Check workspace first
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        # Check built-in
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        self._ensure_cache()
        always = self.get_always_skills()
        if self._cache_always_content is not None and set(skill_names) == set(always):
            return self._cache_always_content
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")
        result = "\n\n---\n\n".join(parts) if parts else ""
        if set(skill_names) == set(always):
            self._cache_always_content = result
        return result

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Returns:
            XML-formatted skills summary.
        """
        self._ensure_cache()
        if self._cache_summary is not None:
            return self._cache_summary
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        always_names = set(self.get_always_skills())
        displayable = [s for s in all_skills if s["name"] not in always_names]
        if not displayable:
            self._cache_summary = ""
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<skills>"]
        for s in displayable:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")
        self._cache_summary = "\n".join(lines)
        return self._cache_summary

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter (supports nanobot and openclaw keys)."""
        try:
            data = json.loads(raw)
            return data.get("nanobot", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get nanobot metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        self._ensure_cache()
        if self._cache_always is not None:
            return list(self._cache_always)
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always") or meta.get("alwaysLoad"):
                result.append(s["name"])
        self._cache_always = result
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None
